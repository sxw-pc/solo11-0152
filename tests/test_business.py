import copy,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from dyebatch import __file__ as imported_file
from dyebatch.store import connect
from dyebatch import recipe,vat,shade
ROOT=Path(__file__).resolve().parents[1]
def fixture(name):return json.loads((ROOT/'fixtures'/(name+'.json')).read_text(encoding='utf8'))
class BusinessTests(unittest.TestCase):
    def setUp(self):self.db=connect(':memory:')
    def tearDown(self):self.db.close()
    def good(self):
        recipe.import_data(self.db,fixture('recipe_ok'))
        vat.import_data(self.db,fixture('vat_ok'))
        shade.import_data(self.db,fixture('shade_ok'))
    def state(self,table,field='state'):
        return self.db.execute('SELECT '+field+' FROM '+table+' WHERE order_id="ORDER-1"').fetchone()[0]
    def test_imported_local(self):
        print('imported module',imported_file)
        self.assertTrue(str(Path(imported_file).resolve()).startswith(str(ROOT.resolve())))
    def test_good_cross_business_path(self):
        self.good()
        self.assertEqual(self.state('recipe_decision'),'approved')
        self.assertEqual(self.db.execute('SELECT state FROM reservation WHERE id="RES-1"').fetchone()[0],'allocated')
        self.assertEqual(self.state('shade_release'),'released')
    def test_customer_signature_and_substitute(self):
        result=recipe.import_data(self.db,fixture('recipe_bad'))[0]
        self.assertEqual(result['state'],'substitute_pending')
        x=fixture('recipe_ok');x['contracts'][0]['signed_by']='factory';x['contracts'][0]['id']='bad-sign';x['revisions'][0]['id']='bad-recipe';x['revisions'][0]['order_id']='ORDER-3';x['contracts'][0]['order_id']='ORDER-3'
        self.assertEqual(recipe.import_data(self.db,x)[0]['state'],'unsigned')
    def test_contract_time_and_dye_lot_are_real_constraints(self):
        x=fixture('recipe_ok')
        x['contracts'][0]['end']='2026-01-09'
        self.assertEqual(recipe.import_data(self.db,x)[0]['state'],'unsigned')
        self.db.close();self.db=connect(':memory:')
        x=fixture('recipe_ok');x['lots'][0]['available_kg']=40
        self.assertEqual(recipe.import_data(self.db,x)[0]['state'],'lot_unavailable')
    def test_parallel_recipe_version_freezes_vat_and_release(self):
        self.good()
        changed=fixture('recipe_ok')['revisions'][0].copy();changed.update(id='RECIPE-CONFLICT',lot='LOT-X')
        recipe.import_data(self.db,{'revisions':[changed]})
        self.assertEqual(self.state('recipe_decision'),'conflict')
        self.assertEqual(self.db.execute('SELECT state FROM reservation WHERE id="RES-1"').fetchone()[0],'held')
        self.assertEqual(self.state('shade_release'),'held')
    def test_withdrawal_releases_future_vat(self):
        self.good()
        rev=fixture('recipe_ok')['revisions'][0].copy();rev.update(id='RECIPE-WITHDRAW',version=2,withdrawn=True)
        recipe.import_data(self.db,{'revisions':[rev]})
        self.assertEqual(self.state('recipe_decision'),'withdrawn')
        self.assertEqual(self.state('shade_release'),'held')
    def test_vat_capacity_and_cleaning_are_separate(self):
        recipe.import_data(self.db,fixture('recipe_ok'))
        vat.import_data(self.db,fixture('vat_bad'))
        self.assertIn('capacity',self.db.execute('SELECT reason FROM reservation WHERE id="RES-2"').fetchone()[0])
        v={'vats':[{'id':'VAT-CLEAN','capacity_kg':120,'previous_shade':'black','cleaned_at':None}],'reservations':[{'id':'RES-CLEAN','order_id':'ORDER-1','vat_id':'VAT-CLEAN','start':'2026-01-20','end':'2026-01-21','qty_kg':90,'priority':1,'at':'2026-01-12'}]}
        vat.import_data(self.db,v)
        self.assertIn('cleaning',self.db.execute('SELECT reason FROM reservation WHERE id="RES-CLEAN"').fetchone()[0])
    def test_bad_instrument_and_color_delta(self):
        recipe.import_data(self.db,fixture('recipe_ok'));vat.import_data(self.db,fixture('vat_ok'))
        shade.import_data(self.db,fixture('shade_bad'))
        self.assertEqual(self.state('shade_release'),'invalid_calibration')
        shade.import_data(self.db,fixture('shade_ok'))
        self.assertEqual(self.state('shade_release'),'released')
        late={'samples':[{'id':'SAMPLE-NEW','order_id':'ORDER-1','instrument_id':'METER-1','at':'2026-01-17','delta_e':2.7,'replicate':1}]}
        shade.import_data(self.db,late)
        self.assertEqual(self.state('shade_release'),'rework')
    def test_rework_cost_independent_history(self):
        self.good()
        shade.import_data(self.db,{'rework':[{'id':'RW-1','order_id':'ORDER-1','dye_kg':3.2,'reason':'corrective dye','approved_by':'manager'}]})
        self.assertEqual(self.db.execute('SELECT dye_kg FROM rework_cost').fetchone()[0],3.2)
        self.assertEqual(self.db.execute('SELECT state FROM rework_liability WHERE cost_id="RW-1"').fetchone()[0],'disputed')
        self.assertEqual(self.state('shade_release'),'released')
    def test_rework_party_follows_documented_cause(self):
        self.good()
        shade.import_data(self.db,{'rework':[{'id':'RW-CLIENT','order_id':'ORDER-1','dye_kg':2.0,'reason':'customer_change','approved_by':'customer'},{'id':'RW-FACTORY','order_id':'ORDER-1','dye_kg':1.0,'reason':'process_error','approved_by':'manager'}]})
        result={r['cost_id']:r['liable_party'] for r in self.db.execute('SELECT * FROM rework_liability')}
        self.assertEqual(result,{'RW-CLIENT':'customer','RW-FACTORY':'factory'})
    def test_failed_shade_holds_future_recipe_change(self):
        self.good()
        shade.import_data(self.db,{'samples':[{'id':'SAMPLE-LATE','order_id':'ORDER-1','instrument_id':'METER-1','at':'2026-01-17','delta_e':3.0,'replicate':1}]})
        self.assertEqual(self.state('shade_release'),'rework')
        self.assertEqual(self.db.execute('SELECT state FROM future_recipe_permission WHERE order_id="ORDER-1"').fetchone()[0],'blocked')
        revision=fixture('recipe_ok')['revisions'][0].copy();revision.update(id='RECIPE-2',version=2,approved_by='factory')
        self.assertEqual(recipe.import_data(self.db,{'revisions':[revision]})[0]['state'],'rework_approval_pending')
    def test_overlapping_vat_priority_changes_winner(self):
        base=fixture('recipe_ok');other=copy.deepcopy(base)
        other['contracts'][0].update(id='CONTRACT-OTHER',order_id='ORDER-OTHER')
        other['revisions'][0].update(id='RECIPE-OTHER',order_id='ORDER-OTHER')
        other['lots']=[]
        recipe.import_data(self.db,base);recipe.import_data(self.db,other)
        vat.import_data(self.db,fixture('vat_ok'))
        claim=fixture('vat_ok')['reservations'][0].copy();claim.update(id='RES-OTHER',order_id='ORDER-OTHER',priority=9)
        vat.import_data(self.db,{'reservations':[claim]})
        self.assertEqual(self.db.execute('SELECT state FROM reservation WHERE id="RES-OTHER"').fetchone()[0],'allocated')
        self.assertEqual(self.db.execute('SELECT state FROM reservation WHERE id="RES-1"').fetchone()[0],'waitlisted')
        withdrawal=fixture('recipe_ok')['revisions'][0].copy()
        withdrawal.update(id='RECIPE-OTHER-WITHDRAW',order_id='ORDER-OTHER',version=2,withdrawn=True)
        recipe.import_data(self.db,{'revisions':[withdrawal]})
        self.assertEqual(self.db.execute('SELECT state FROM reservation WHERE id="RES-1"').fetchone()[0],'allocated')
    def test_event_conflict_rolls_back(self):
        self.good();before=self.db.execute('SELECT count(*) FROM evidence').fetchone()[0]
        x=fixture('recipe_ok');x['contracts'][0]['shade']='red'
        with self.assertRaises(ValueError):recipe.import_data(self.db,x)
        self.assertEqual(self.db.execute('SELECT count(*) FROM evidence').fetchone()[0],before)
    def test_cli_demo_audit(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'demo.sqlite')
            for cmd in ('demo','audit'):
                r=subprocess.run([sys.executable,'-m','dyebatch',cmd,'--db',path],cwd=ROOT,capture_output=True,text=True)
                self.assertEqual(r.returncode,0,r.stderr)
                if cmd=='audit':self.assertTrue(json.loads(r.stdout)['ok'])
    def test_cli_each_business_entry_and_bad_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'entry.sqlite')
            for command,name in [('recipe-import','recipe_ok'),('vat-plan','vat_ok'),('shade-import','shade_bad')]:
                r=subprocess.run([sys.executable,'-m','dyebatch',command,'--db',path,'--input',str(ROOT/'fixtures'/(name+'.json'))],cwd=ROOT,capture_output=True,text=True)
                self.assertEqual(r.returncode,0,r.stderr)
            r=subprocess.run([sys.executable,'-m','dyebatch','report','--db',path],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(json.loads(r.stdout)[0]['shade']['state'],'invalid_calibration')
if __name__=='__main__':unittest.main()
