import hashlib,json
from pathlib import Path

def read(path):
    raw=Path(path).read_bytes();return json.loads(raw),raw

def canonical_bytes(item):
    return json.dumps(item,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()

def digest_of(item):
    return hashlib.sha256(canonical_bytes(item)).hexdigest()

def record(db,kind,item):
    raw=canonical_bytes(item)
    sha=hashlib.sha256(raw).hexdigest();id=item['id']
    old=db.execute('SELECT sha FROM evidence WHERE id=?',(id,)).fetchone()
    if old:
        if old['sha']!=sha:raise ValueError('evidence identity conflict: '+id)
        return False
    db.execute('INSERT INTO evidence VALUES(?,?,?,?)',(id,kind,sha,raw));return True

def file_record(db,command,raw):
    sha=hashlib.sha256(raw).hexdigest()
    db.execute('INSERT INTO file_journal(command,sha,raw) VALUES(?,?,?)',(command,sha,raw))

SECTION_KIND={
    'recipe-import':{'contracts':'contract','lots':'lot','revisions':'recipe'},
    'vat-plan':{'vats':'vat','reservations':'reservation'},
    'shade-import':{'instruments':'instrument','samples':'sample','rework':'rework'},
}

def payload_items(command,data):
    """Flatten one import payload into (kind,item) pairs in physical order."""
    return [(kind,item) for section,kind in SECTION_KIND[command].items()
            for item in data.get(section,[])]

def _evidence_map(db):
    return {row['id']:dict(row) for row in db.execute('SELECT * FROM evidence')}

def verify_handoff_file(db,command,raw,attested_sha):
    """Type-1 recovery: an original whole file returned from a trusted handoff
    archive. The shift-attested sha must match the real bytes byte-for-byte;
    every normalized item inside must match surviving evidence by identity and
    digest. A re-serialized/saved-as JSON whose digest differs is rejected, so
    such a copy can never be claimed to be the original bytes."""
    result={'kind':'handoff_file','command':command,'attested_sha':attested_sha,
            'valid':False,'errors':[], 'items':[]}
    if not isinstance(raw,(bytes,bytearray)):
        result['errors'].append('handoff evidence must be original file bytes');return result
    actual=hashlib.sha256(bytes(raw)).hexdigest()
    result['actual_sha']=actual
    if actual!=attested_sha:
        result['errors'].append(
            'file digest %s does not match shift-attested digest %s; bytes are not the attested original'%(actual,attested_sha))
        return result
    try:data=json.loads(bytes(raw))
    except Exception as exc:
        result['errors'].append('attested bytes are not parseable JSON: %s'%exc);return result
    if not isinstance(data,dict):
        result['errors'].append('attested payload is not a JSON object');return result
    if command not in SECTION_KIND:
        result['errors'].append('unknown command: %s'%command);return result
    known=_evidence_map(db)
    seen=set()
    for kind,item in payload_items(command,data):
        if not isinstance(item,dict) or 'id' not in item:
            result['errors'].append('payload item without identity');return result
        ident=item['id'];sha=digest_of(item)
        row=known.get(ident)
        if row is None:
            result['errors'].append('item %s is not part of surviving evidence'%ident);return result
        if row['kind']!=kind:
            result['errors'].append('item %s kind mismatch: %s != %s'%(ident,kind,row['kind']));return result
        if row['sha']!=sha:
            result['errors'].append('item %s digest does not match surviving evidence'%ident);return result
        if ident in seen:
            result['errors'].append('item %s appears more than once in the file'%ident);return result
        seen.add(ident)
        result['items'].append({'id':ident,'kind':kind,'sha':sha})
    if not seen:
        result['errors'].append('attested file contains no evidence items');return result
    result['valid']=True
    return result

def verify_boundary_manifest(db,manifest):
    """Type-2 recovery: a signed transaction-boundary receipt describing, by
    concrete original-item digests, which two items shared one submission or
    sat in adjacent submissions, with surviving before/after anchors. It never
    carries whole-file bytes and therefore can never fabricate a file journal."""
    result={'kind':'boundary_manifest','valid':False,'errors':[],
            'relations':[],'anchors':[]}
    if not isinstance(manifest,dict):
        result['errors'].append('manifest must be a signed object');return result
    relations=manifest.get('relations')
    anchors=manifest.get('anchors',[])
    if not isinstance(relations,list) or not relations:
        result['errors'].append('manifest needs at least one signed relation');return result
    if not isinstance(anchors,list):
        result['errors'].append('manifest anchors must be a list');return result
    known=_evidence_map(db)
    def by_digest(sha):
        for row in known.values():
            if row['sha']==sha:return row
        return None
    pairs=set()
    for rel in relations:
        if not isinstance(rel,dict):
            result['errors'].append('relation must be an object');return result
        mode=rel.get('mode');a=rel.get('item_sha');b=rel.get('other_item_sha')
        if mode not in ('same','adjacent'):
            result['errors'].append('relation mode must be "same" or "adjacent"');return result
        ra,rb=by_digest(a),by_digest(b)
        if ra is None or rb is None:
            result['errors'].append('relation references digest absent from surviving evidence');return result
        if ra['id']==rb['id']:
            result['errors'].append('relation cannot reference one item twice');return result
        key=(tuple(sorted((a,b))),mode)
        if key in pairs:
            result['errors'].append('duplicate relation between the same two items');return result
        pairs.add(key)
        result['relations'].append({'mode':mode,'item_id':ra['id'],'other_item_id':rb['id'],
                                    'item_sha':a,'other_item_sha':b})
    for anchor in anchors:
        if not isinstance(anchor,dict) or anchor.get('role') not in ('before','after'):
            result['errors'].append('anchor role must be "before" or "after"');return result
        row=by_digest(anchor.get('item_sha'))
        if row is None:
            result['errors'].append('anchor digest absent from surviving evidence');return result
        result['anchors'].append({'role':anchor['role'],'item_id':row['id'],'item_sha':anchor['item_sha']})
    result['valid']=True
    return result
