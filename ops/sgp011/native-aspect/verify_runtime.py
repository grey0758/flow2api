"""Read-only equivalence check; never imports application modules or makes requests."""
import argparse, ast, hashlib, json, pathlib
def sha(b):return hashlib.sha256(b).hexdigest()
def verify(root,manifest):
 module=root/'src/services/original_aspect.py'
 assert sha(module.read_bytes())==manifest['original_aspect_sha256'],'aspect_module_differs'
 tree=ast.parse((root/'src/services/cookie_media.py').read_text());got={}
 for node in tree.body:
  key=None
  if isinstance(node,ast.FunctionDef)and node.name in {'context','image_argument'}:key=node.name
  elif isinstance(node,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='ASPECTS'for t in node.targets):key='ASPECTS'
  elif isinstance(node,ast.ImportFrom)and node.module=='original_aspect':key='original_aspect_import'
  if key:got[key]=sha(ast.dump(node,include_attributes=False).encode())
 assert got==manifest['wire_ast_sha256'],'wire_aspect_contract_differs'
 return {'verified':True,'exact_aspect_module':True,'wire_ast_contracts':sorted(got),'application_imported':False,'network_requests':0,'business_writes':0}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=pathlib.Path,required=True);p.add_argument('--manifest',type=pathlib.Path,default=pathlib.Path(__file__).with_name('manifest.json'));args=p.parse_args()
 print(json.dumps(verify(args.root,json.loads(args.manifest.read_text()))))
