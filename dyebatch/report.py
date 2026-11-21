import json
from .store import connect
from . import recipe,vat,shade
def build(db):
    orders=[r[0] for r in db.execute('SELECT DISTINCT order_id FROM recipe_revision UNION SELECT DISTINCT order_id FROM reservation UNION SELECT DISTINCT order_id FROM shade_sample ORDER BY order_id')]
    return [{'order_id':o,'recipe':dict(x) if (x:=db.execute('SELECT * FROM recipe_decision WHERE order_id=?',(o,)).fetchone()) else None,'reservations':[dict(x) for x in db.execute('SELECT * FROM reservation WHERE order_id=?',(o,))],'shade':dict(x) if (x:=db.execute('SELECT * FROM shade_release WHERE order_id=?',(o,)).fetchone()) else None,'rework_kg':db.execute('SELECT COALESCE(SUM(dye_kg),0) FROM rework_cost WHERE order_id=?',(o,)).fetchone()[0],'rework_liability':[dict(x) for x in db.execute('SELECT * FROM rework_liability WHERE order_id=?',(o,))],'history':[dict(x) for x in db.execute('SELECT subsystem,state,reason,source_id FROM decision_history WHERE order_id=? ORDER BY id',(o,))]} for o in orders]
def audit(db):
    replay=connect(':memory:');handlers={'recipe-import':recipe.import_data,'vat-plan':vat.import_data,'shade-import':shade.import_data}
    for row in db.execute('SELECT command,raw FROM file_journal ORDER BY id'):
        handlers[row['command']](replay,json.loads(row['raw']))
    equal=build(db)==build(replay)
    replay.close()
    return {'ok':equal,'files':db.execute('SELECT count(*) FROM file_journal').fetchone()[0]}
