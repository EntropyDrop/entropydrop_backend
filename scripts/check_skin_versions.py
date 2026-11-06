import os,sys,json,asyncio,io,unittest
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
os.environ.update(DATABASE_URL='sqlite://',REDIS_URL='redis://localhost:6379/15',AWS_EC2_METADATA_DISABLED='true',JWT_SECRET_KEY='test-key-only-at-least-thirty-two-chars',SPACE_SERVICE_URL='')
import redis
fake=MagicMock();fake.get.return_value=None
redis.Redis.from_url=classmethod(lambda cls,*a,**kw:fake)
from main import app
from fastapi.testclient import TestClient
from auth import get_current_user
from database import get_db
from pipeline_registry import get_pipeline,is_sking_ddj_model
import routers.generate as g
from PIL import Image

class VersionedReleaseTests(unittest.TestCase):
 def setUp(self):
  app.state.limiter.enabled=False
  app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id='cutover-test',is_pro_active=True,credits=100)
  app.dependency_overrides[get_db]=lambda:MagicMock()
  self.client=TestClient(app)
 def tearDown(self):app.dependency_overrides.clear()
 def test_catalog(self):
  d=self.client.get('/skin/api/models').json()
  self.assertEqual(d['image_to_skin_models'],['SKING_DDJ_v131','SKING_DDJ_v130'])
  self.assertEqual(d['text_to_skin_options'],[]);self.assertEqual(d['image_edit_to_skin_options'],[])
  self.assertEqual(d['image_to_skin_options'][0]['model_version'],'SKING_DDJ_v131')
 def test_pipeline(self):
  a=get_pipeline('SKING_DDJ_v130').to_task_payload();b=get_pipeline('SKING_DDJ_v131').to_task_payload()
  self.assertEqual(b['dense_uv_checkpoint_file'],'SKING_DDJ_v131/parser.pt');self.assertEqual(b['DMR_mappings_dir'],'SKING_DDJ_v131/mappings')
  for key in a:
   if key not in ['dense_uv_checkpoint_file','DMR_mappings_dir']:self.assertEqual(a[key],b[key])
  self.assertEqual(a['provider_model'],'nano-banana-pro')
  p=get_pipeline('SKING_DDJ_v130').to_task_payload();self.assertEqual(p['dense_uv_checkpoint_file'],'SKING_DDJ_v130/parser.pt');self.assertEqual(p['DMR_mappings_dir'],'SKING_DDJ_v130/mappings');self.assertTrue(is_sking_ddj_model('SKING_DDJ_v130'))
 def test_historical_pipeline_preserved(self):
  for v in ['SKING_DDJ_v101c','SKING_DDJ_v104b']:self.assertTrue(is_sking_ddj_model(v));self.assertTrue(get_pipeline(v).dense_uv_checkpoint_file)
 def test_retired_rejected_in_all_modes(self):
  with patch.object(g.backend_utils,'is_text_to_skin_enabled',return_value=True),patch.object(g.backend_utils,'is_image_to_skin_enabled',return_value=True),patch.object(g.backend_utils,'is_image_edit_to_skin_enabled',return_value=True):
   for mode,aux in [('aigc_image_to_skin',''),('aigc_text_to_skin','z_image'),('aigc_image_edit_to_skin','flux_4b')]:
    for version in ['sking_v73_flux_4b_000027000','SKING_DDJ_v101c','SKING_DDJ_v104b','SKING_DDJ_v54']:
     r=self.client.post('/skin/api/generate',data={'mode':mode,'model_version':version,'aux_model_version':aux,'prompt':'test'})
     self.assertEqual(r.status_code,400,r.text);self.assertIn('Invalid model version',r.json()['detail'])
 def test_generation_switch_still_honored(self):
  with patch.object(g.backend_utils,'is_image_to_skin_enabled',return_value=False):
   r=self.client.post('/skin/api/generate',data={'mode':'aigc_image_to_skin','model_version':'SKING_DDJ_v130'})
   self.assertEqual(r.status_code,403);self.assertIn('maintenance',r.text)
 def test_stage1_queue_payload(self):
  log=SimpleNamespace(id='probe',source='uploads/probe.png',mode='aigc_image_to_skin',model_version='SKING_DDJ_v131',is_public=False,pro_priority=False,is_pro=False)
  with patch.object(g,'Queue') as queue:
   g.enqueue_real_to_render_task(log,False,'image/png')
   args=queue.return_value.enqueue.call_args.kwargs['args']
   self.assertEqual(args[5],'SKING_DDJ_v131');self.assertEqual(args[6]['DMR_mappings_dir'],'SKING_DDJ_v131/mappings');self.assertEqual(queue.call_args.args[0],'queue_real_to_render')
 def test_stage2_queue_payload(self):
  log=SimpleNamespace(id='probe',source='uploads/probe.png',mode='aigc_image_to_skin',model_version='SKING_DDJ_v131',is_public=False,pro_priority=False,is_pro=False,image_to_skin_edited_result='generations/probe-edited.png')
  with patch.object(g,'Queue') as queue:
   g.enqueue_render_to_uv_task(log,False)
   args=queue.return_value.enqueue.call_args.kwargs['args']
   self.assertIn('SKING_DDJ_v131',args);self.assertIn('SKING_DDJ_v131/parser.pt',args);self.assertIn('SKING_DDJ_v131/mappings',args)
if __name__=='__main__':
 result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(VersionedReleaseTests))
 if not result.wasSuccessful():sys.exit(1)
 print('V131_CHECKS='+json.dumps({'passed':True,'tests':result.testsRun,'catalog':asyncio.run(g.get_models(current_user=object()))}))
