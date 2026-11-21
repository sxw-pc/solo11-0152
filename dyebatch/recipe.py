from .source import record
from .store import history
def current(db,order_id):return db.execute('SELECT * FROM recipe_decision WHERE order_id=?',(order_id,)).fetchone()
def decide(db,order_id):
    revisions=db.execute('SELECT * FROM recipe_revision WHERE order_id=? ORDER BY version DESC,id',(order_id,)).fetchall()
    if not revisions:return None
    latest=revisions[0];same=[r for r in revisions if r['version']==latest['version']]
    contract=db.execute('SELECT * FROM customer_contract WHERE order_id=? AND start<=? AND end>=? ORDER BY start DESC',(order_id,latest['at'],latest['at'])).fetchone()
    lot=db.execute('SELECT * FROM dye_lot WHERE id=?',(latest['lot'],)).fetchone()
    if len({(r['lot'],r['shade'],r['qty_kg'],r['withdrawn']) for r in same})>1:state,reason,chosen='conflict','parallel recipe revisions disagree',None
    elif latest['withdrawn']:state,reason,chosen='withdrawn','current recipe withdrawn',None
    elif not contract or contract['signed_by']!='customer':state,reason,chosen='unsigned','customer shade contract missing at recipe time',None
    elif contract['shade']!=latest['shade']:state,reason,chosen='shade_mismatch','recipe shade differs from signed sample',None
    elif not lot or lot['shade']!=latest['shade'] or lot['expires']<latest['at'] or lot['available_kg']<latest['qty_kg']:state,reason,chosen='lot_unavailable','dye lot unavailable or incompatible',None
    elif latest['substitute_of'] and latest['approved_by']!='customer':state,reason,chosen='substitute_pending','replacement dye lot needs customer approval',None
    elif latest['version']>1 and (hold:=db.execute('SELECT * FROM future_recipe_permission WHERE order_id=?',(order_id,)).fetchone()) and hold['state']=='blocked' and latest['approved_by']!='customer':state,reason,chosen='rework_approval_pending','failed shade lot needs customer reapproval',None
    else:state,reason,chosen='approved','contract, lot and substitution approved',latest['id']
    old=current(db,order_id)
    if old and (old['state'],old['reason'],old['recipe_id'])==(state,reason,chosen):return dict(old)
    version=(old['version']+1) if old else 1
    db.execute('INSERT INTO recipe_decision VALUES(?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,recipe_id=excluded.recipe_id,version=excluded.version',(order_id,state,reason,chosen,version))
    history(db,'recipe',order_id,state,reason,latest['id'])
    from .vat import reconcile_order
    reconcile_order(db,order_id)
    from .shade import reconcile_order as reconcile_release
    reconcile_release(db,order_id)
    return dict(current(db,order_id))
def import_data(db,data):
    touched=set()
    with db:
        for c in data.get('contracts',[]):
            if record(db,'contract',c):db.execute('INSERT INTO customer_contract VALUES(?,?,?,?,?,?,?,?)',(c['id'],c['customer'],c['order_id'],c['shade'],c['start'],c['end'],c['threshold'],c['signed_by']))
            touched.add(c['order_id'])
        for l in data.get('lots',[]):
            if record(db,'lot',l):db.execute('INSERT INTO dye_lot VALUES(?,?,?,?)',(l['id'],l['shade'],l['available_kg'],l['expires']))
            touched.update(r['order_id'] for r in db.execute('SELECT order_id FROM recipe_revision WHERE lot=?',(l['id'],)))
        for r in data.get('revisions',[]):
            if record(db,'recipe',r):db.execute('INSERT INTO recipe_revision VALUES(?,?,?,?,?,?,?,?,?,?)',(r['id'],r['order_id'],r['version'],r['at'],r['lot'],r.get('substitute_of'),r.get('approved_by'),int(r.get('withdrawn',False)),r['qty_kg'],r['shade']))
            touched.add(r['order_id'])
        return [decide(db,o) for o in sorted(touched) if db.execute('SELECT 1 FROM recipe_revision WHERE order_id=?',(o,)).fetchone()]
