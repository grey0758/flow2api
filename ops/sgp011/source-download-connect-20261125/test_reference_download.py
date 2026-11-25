import ast,asyncio,contextlib,hashlib,importlib.util,io,json,pathlib,types,unittest
from unittest.mock import AsyncMock
ROOT=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('image_diagnostics',ROOT/'image_diagnostics.py')
diagnostics=importlib.util.module_from_spec(spec);spec.loader.exec_module(diagnostics)
class CurlError(Exception):
 def __init__(self,code,response=None):self.code=code;self.response=response;super().__init__('curl failure')
class Response:
 def __init__(self,status=200,content=b'original-image-bytes',headers=None):
  self.status_code=status;self.content=content;self.headers=headers or {};self.infos={}
class Clock:
 def __init__(self):self.now=0
 def monotonic(self):return self.now
class TestDownload(unittest.IsolatedAsyncioTestCase):
 async def alternate(self,url,remaining):return {},{'dns_route':'system'}
 def make(self,outcomes,advance=0,source='routes.py'):
  tree=ast.parse((ROOT/source).read_text());fn=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='retrieve_image_data')
  clock=Clock();calls=[];sessions=[];closed=[]
  class Session:
   def __init__(self,**kwargs):sessions.append(kwargs)
   async def __aenter__(self):return self
   async def __aexit__(self,*args):closed.append(True)
   async def get(self,url,**kwargs):
    calls.append((url,kwargs));clock.now+=advance
    out=outcomes.pop(0)
    if isinstance(out,BaseException):raise out
    return out
  logger=types.SimpleNamespace(log_warning=lambda *a:None,log_error=lambda *a:None)
  ns={'Optional':__import__('typing').Optional,'generation_handler':None,'AsyncSession':Session,'asyncio':asyncio,'hashlib':hashlib,'time':clock,'json':json,'debug_logger':logger,'CURL_INFOS':diagnostics.CURL_INFOS,'begin_transfer':diagnostics.begin_transfer,'finish_transfer':diagnostics.finish_transfer,'emit_transfer_failure':diagnostics.emit_transfer_failure,'curl_code':diagnostics.curl_code,'alternate_source_transport':self.alternate,'CurlOpt':__import__('curl_cffi.const',fromlist=['CurlOpt']).CurlOpt,'CurlECode':__import__('curl_cffi.const',fromlist=['CurlECode']).CurlECode,'CurlHttpVersion':__import__('curl_cffi.const',fromlist=['CurlHttpVersion']).CurlHttpVersion}
  exec(compile(ast.Module(body=[fn],type_ignores=[]),str(ROOT/source),'exec'),ns)
  return ns['retrieve_image_data'],calls,sessions,closed
 async def call(self,outcomes,**options):
  fn,calls,sessions,closed=self.make(outcomes,**options)
  capture=io.StringIO()
  with contextlib.redirect_stdout(capture):result=await fn('https://private-source.example/image.jpg?secret=never-log')
  self.assertNotIn('private-source.example',capture.getvalue());self.assertNotIn('secret=never-log',capture.getvalue())
  return result,calls,sessions,closed,capture.getvalue()
 async def test_original_bytes_return_once(self):
  original=b'\xff\xd8\xff\x00unchanged-exif-and-color'
  result,calls,sessions,closed,log=await self.call([Response(content=original)])
  self.assertEqual(result,original);self.assertEqual(len(calls),1);self.assertEqual(len(closed),1);self.assertEqual(log,'')
  self.assertEqual(sessions[0]['trust_env'],False);self.assertEqual(calls[0][1]['proxies'],{'http':'','https':''})
 async def test_timeout_partial_then_success(self):
  result,calls,_,closed,log=await self.call([CurlError(28,Response(content=b'partial')),Response(content=b'complete')])
  self.assertEqual(result,b'complete');self.assertEqual(len(calls),2);self.assertEqual(len(closed),2)
  self.assertIn('"recovered":true',log);self.assertIn('"curl_code":28',log)
 async def test_retry_time_left_is_bounded(self):
  _,calls,_,_,_=await self.call([CurlError(28),Response()],advance=60.1)
  self.assertAlmostEqual(calls[0][1]['timeout'],60);self.assertAlmostEqual(calls[1][1]['timeout'],29.9)
 async def test_total_budget_prevents_second_get(self):
  result,calls,_,_,_=await self.call([CurlError(28)],advance=90.1)
  self.assertIsNone(result);self.assertEqual(len(calls),1)
 async def test_only_two_gets(self):
  result,calls,_,_,_=await self.call([CurlError(28),CurlError(28),Response()])
  self.assertIsNone(result);self.assertEqual(len(calls),2)
 async def test_source_503_then_success(self):
  result,calls,_,_,_=await self.call([Response(503,b'busy'),Response()])
  self.assertEqual(result,b'original-image-bytes');self.assertEqual(len(calls),2)
 async def test_permanent_http_errors_do_not_retry(self):
  for status in (400,401,403,404,410,422):
   with self.subTest(status=status):
    result,calls,_,_,_=await self.call([Response(status,b'error'),Response()])
    self.assertIsNone(result);self.assertEqual(len(calls),1)
 async def test_tls_certificate_error_does_not_retry(self):
  result,calls,_,_,_=await self.call([CurlError(60),Response()]);self.assertIsNone(result);self.assertEqual(len(calls),1)
 async def test_empty_response_retries_without_delivering_empty(self):
  result,calls,_,_,_=await self.call([Response(content=b''),Response()]);self.assertEqual(result,b'original-image-bytes');self.assertEqual(len(calls),2)
 async def test_short_200_body_is_discarded(self):
  result,calls,_,_,_=await self.call([Response(content=b'cut',headers={'Content-Length':'99'}),Response(content=b'original')]);self.assertEqual(result,b'original');self.assertEqual(len(calls),2)
 async def test_encoded_content_length_is_not_decoded_length(self):
  result,calls,_,_,_=await self.call([Response(content=b'decoded-body',headers={'Content-Length':'5','Content-Encoding':'gzip'})]);self.assertEqual(result,b'decoded-body');self.assertEqual(len(calls),1)
 async def test_external_cancellation_never_retries(self):
  fn,calls,_,closed=self.make([asyncio.CancelledError(),Response()])
  with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(asyncio.CancelledError):await fn('https://example.com/image')
  self.assertEqual(len(calls),1);self.assertEqual(len(closed),1)
 async def test_output_download_does_not_gain_retry(self):
  fn,calls,_,_=self.make([CurlError(28),Response()])
  with contextlib.redirect_stdout(io.StringIO()):result=await fn('https://example.com/output',diagnostic_stage='gemini_output_download')
  self.assertIsNone(result);self.assertEqual(len(calls),1)
 async def test_old_code_preserves_existing_alternate_behavior(self):
  self.alternate=AsyncMock(return_value=({'curl_options':{'resolve':'chosen'},'verify':True},{'dns_route':'public_doh_tls'}))
  fn,calls,_,_=self.make([CurlError(28),Response()],source='routes-baseline.py')
  with contextlib.redirect_stdout(io.StringIO()):result=await fn('https://example.com/source')
  self.assertEqual(result,b'original-image-bytes');self.alternate.assert_awaited_once()
 async def test_retry_uses_new_dns_and_tls(self):
  self.alternate=AsyncMock(return_value=({'curl_options':{'resolve':'chosen'},'verify':True},{'dns_route':'public_doh_tls'}))
  result,calls,sessions,closed,log=await self.call([CurlError(28),Response()])
  self.alternate.assert_awaited_once();self.assertEqual(sessions[1]['curl_options']['resolve'],'chosen')
  self.assertTrue(calls[1][1]['verify']);self.assertEqual(sessions[0]['curl_options'][__import__('curl_cffi.const',fromlist=['CurlOpt']).CurlOpt.CONNECTTIMEOUT_MS],8000);self.assertEqual(result,b'original-image-bytes')
  self.assertIn('public_doh_tls',log)
 async def test_connect_budget_is_8s_but_body_total_stays_60s(self):
  result,calls,sessions,_,_=await self.call([Response()])
  from curl_cffi.const import CurlOpt
  self.assertEqual(sessions[0]['curl_options'][CurlOpt.CONNECTTIMEOUT_MS],8000)
  self.assertEqual(calls[0][1]['timeout'],60)
 async def test_output_does_not_inherit_input_connect_policy(self):
  fn,calls,sessions,_=self.make([Response()])
  result=await fn('https://example.com/output',diagnostic_stage='gemini_output_download')
  self.assertEqual(result,b'original-image-bytes');self.assertNotIn('curl_options',sessions[0])
 async def test_early_connect_timeout_leaves_budget_for_verified_retry(self):
  self.alternate=AsyncMock(return_value=({'curl_options':{'resolve':'chosen'},'verify':True},{'dns_route':'public_doh_tls'}))
  result,calls,sessions,_,_=await self.call([CurlError(28),Response()],advance=8)
  self.assertEqual(result,b'original-image-bytes');self.assertEqual(calls[1][1]['timeout'],60)
  self.assertTrue(calls[1][1]['verify'])
if __name__=='__main__' :unittest.main(verbosity=2)
