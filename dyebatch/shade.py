from .source import record
from .store import history
def decide_rework(db,cost_id):
    item=db.execute('SELECT * FROM rework_cost WHERE id=?',(cost_id,)).fetchone()
    if item['dye_kg']<=0:party,state,reason='none','disputed','extra dye quantity must be positive'
    elif item['reason']=='customer_change' and item['approved_by']=='customer':party,state,reason='customer','assigned','customer-signed shade change'
    elif item['reason']=='process_error' and item['approved_by']=='manager':party,state,reason='factory','assigned','factory process correction'
    else:party,state,reason='unassigned','disputed','cause or authorizing role missing'
    db.execute('INSERT INTO rework_liability VALUES(?,?,?,?,?)',(cost_id,item['order_id'],party,state,reason))
    history(db,'rework',item['order_id'],state,reason,cost_id)
    return {'cost_id':cost_id,'liable_party':party,'state':state,'reason':reason}
def reconcile_order(db,order_id):
    samples=db.execute('SELECT * FROM shade_sample WHERE order_id=? ORDER BY at DESC,id',(order_id,)).fetchall()
    recipe=db.execute('SELECT * FROM recipe_decision WHERE order_id=?',(order_id,)).fetchone()
    allocated=db.execute('SELECT * FROM reservation WHERE order_id=? AND state="allocated" ORDER BY start LIMIT 1',(order_id,)).fetchone()
    contract=db.execute('SELECT * FROM customer_contract WHERE order_id=? ORDER BY start DESC LIMIT 1',(order_id,)).fetchone()
    old=db.execute('SELECT * FROM shade_release WHERE order_id=?',(order_id,)).fetchone()
    current=[s for s in samples if s['kind']=='measurement']
    by_rep={}
    for s in current:
        if s['replicate'] in (1,2):by_rep.setdefault(s['replicate'],s)
    if not recipe or recipe['state']!='approved':state,reason='held','recipe evidence not approved'
    elif not allocated:state,reason='held','no allocated dye vat'
    elif len(by_rep)<2:state,reason='pending_samples','two independent replicate measurements required'
    elif any(not(db.execute('SELECT 1 FROM colorimeter WHERE id=? AND valid_from<=? AND valid_to>=?',(s['instrument_id'],s['at'],s['at'])).fetchone()) for s in by_rep.values()):state,reason='invalid_calibration','instrument certificate does not cover sampling time'
    elif any(s['delta_e']>contract['threshold'] for s in by_rep.values()):state,reason='rework','sample exceeds signed shade tolerance'
    else:state,reason='released','two calibrated samples satisfy customer tolerance'
    if old and (old['state'],old['reason'],old['recipe_id'],old['reservation_id'])==(state,reason,recipe['recipe_id'] if recipe else None,allocated['id'] if allocated else None):return dict(old)
    version=old['version']+1 if old else 1
    db.execute('INSERT INTO shade_release VALUES(?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,recipe_id=excluded.recipe_id,reservation_id=excluded.reservation_id,version=excluded.version',(order_id,state,reason,recipe['recipe_id'] if recipe else None,allocated['id'] if allocated else None,version))
    history(db,'shade',order_id,state,reason,current[0]['id'] if current else None)
    if state=='rework':
        db.execute('INSERT INTO future_recipe_permission VALUES(?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,shade_version=excluded.shade_version',(order_id,'blocked','shade failure requires customer consent for future recipe',version))
    return dict(db.execute('SELECT * FROM shade_release WHERE order_id=?',(order_id,)).fetchone())
def import_data(db,data):
    touched=set()
    with db:
        for i in data.get('instruments',[]):
            if record(db,'instrument',i):db.execute('INSERT INTO colorimeter VALUES(?,?,?,?)',(i['id'],i['valid_from'],i['valid_to'],i['operator']))
            touched.update(x['order_id'] for x in db.execute('SELECT order_id FROM shade_sample WHERE instrument_id=?',(i['id'],)))
        for s in data.get('samples',[]):
            if record(db,'sample',s):db.execute('INSERT INTO shade_sample VALUES(?,?,?,?,?,?,?)',(s['id'],s['order_id'],s['instrument_id'],s['at'],s['delta_e'],s['replicate'],s.get('kind','measurement')))
            touched.add(s['order_id'])
        for r in data.get('rework',[]):
            if record(db,'rework',r):
                db.execute('INSERT INTO rework_cost VALUES(?,?,?,?,?)',(r['id'],r['order_id'],r['dye_kg'],r['reason'],r.get('approved_by')))
                decide_rework(db,r['id'])
            touched.add(r['order_id'])
        return [reconcile_order(db,o) for o in sorted(touched)]
