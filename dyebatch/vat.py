from .source import record
from .store import history

def overlap(a,b):
    return a['start'] < b['end'] and b['start'] < a['end']

def _eligibility(db,claim):
    rec=db.execute('SELECT * FROM recipe_decision WHERE order_id=?',(claim['order_id'],)).fetchone()
    vat=db.execute('SELECT * FROM vat WHERE id=?',(claim['vat_id'],)).fetchone()
    if not rec or rec['state']!='approved':return 'held','recipe not approved'
    if not vat:return 'held','vat missing'
    revision=db.execute('SELECT * FROM recipe_revision WHERE id=?',(rec['recipe_id'],)).fetchone()
    if claim['qty_kg']>vat['capacity_kg'] or claim['qty_kg']>revision['qty_kg']:
        return 'held','vat capacity or approved recipe quantity exceeded'
    if vat['previous_shade']!=revision['shade'] and (not vat['cleaned_at'] or vat['cleaned_at']>claim['start']):
        return 'held','dark-to-light cleaning witness missing'
    return 'eligible','eligible for vat window'

def reconcile_vat(db,vat_id):
    claims=db.execute('SELECT * FROM reservation WHERE vat_id=? ORDER BY priority DESC,at,id',(vat_id,)).fetchall()
    allocated=[];changes=[]
    for claim in claims:
        state,reason=_eligibility(db,claim)
        if state=='eligible':
            if any(overlap(claim,other) for other in allocated):state,reason='waitlisted','higher priority reservation owns vat'
            else:state,reason='allocated','vat window allocated';allocated.append(claim)
        if (claim['state'],claim['reason'])!=(state,reason):
            db.execute('UPDATE reservation SET state=?,reason=? WHERE id=?',(state,reason,claim['id']))
            history(db,'vat',claim['order_id'],state,reason,claim['id'])
            changes.append(claim['order_id'])
    from .shade import reconcile_order as release
    for order_id in sorted(set(changes)):
        release(db,order_id)
    return changes

def reconcile_order(db,order_id):
    vat_ids=[r['vat_id'] for r in db.execute('SELECT DISTINCT vat_id FROM reservation WHERE order_id=?',(order_id,))]
    for vat_id in vat_ids:reconcile_vat(db,vat_id)

def import_data(db,data):
    vat_ids=set()
    with db:
        for v in data.get('vats',[]):
            if record(db,'vat',v):
                db.execute('INSERT INTO vat VALUES(?,?,?,?)',(v['id'],v['capacity_kg'],v['previous_shade'],v.get('cleaned_at')))
            vat_ids.add(v['id'])
        for r in data.get('reservations',[]):
            if record(db,'reservation',r):
                db.execute('INSERT INTO reservation VALUES(?,?,?,?,?,?,?,?,?,?)',(r['id'],r['order_id'],r['vat_id'],r['start'],r['end'],r['qty_kg'],r['priority'],r['at'],'pending','not evaluated'))
            vat_ids.add(r['vat_id'])
        for vat_id in sorted(vat_ids):reconcile_vat(db,vat_id)
        if not vat_ids:return []
        return [dict(r) for r in db.execute('SELECT * FROM reservation WHERE vat_id IN ('+','.join('?' for _ in vat_ids)+') ORDER BY id',tuple(sorted(vat_ids)))]
