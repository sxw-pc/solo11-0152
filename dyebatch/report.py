import json
from .store import connect
from . import recipe,vat,shade
from .source import SECTION_KIND,payload_items,verify_handoff_file,verify_boundary_manifest

HANDLERS={'recipe-import':recipe.import_data,'vat-plan':vat.import_data,'shade-import':shade.import_data}

def build(db):
    orders=[r[0] for r in db.execute('SELECT DISTINCT order_id FROM recipe_revision UNION SELECT DISTINCT order_id FROM reservation UNION SELECT DISTINCT order_id FROM shade_sample ORDER BY order_id')]
    return [{'order_id':o,'recipe':dict(x) if (x:=db.execute('SELECT * FROM recipe_decision WHERE order_id=?',(o,)).fetchone()) else None,'reservations':[dict(x) for x in db.execute('SELECT * FROM reservation WHERE order_id=?',(o,))],'shade':dict(x) if (x:=db.execute('SELECT * FROM shade_release WHERE order_id=?',(o,)).fetchone()) else None,'rework_kg':db.execute('SELECT COALESCE(SUM(dye_kg),0) FROM rework_cost WHERE order_id=?',(o,)).fetchone()[0],'rework_liability':[dict(x) for x in db.execute('SELECT * FROM rework_liability WHERE order_id=?',(o,))],'history':[dict(x) for x in db.execute('SELECT subsystem,state,reason,source_id FROM decision_history WHERE order_id=? ORDER BY id',(o,))]} for o in orders]

def _legacy_replay(db):
    replay=connect(':memory:')
    try:
        for row in db.execute('SELECT command,raw FROM file_journal ORDER BY id'):
            HANDLERS[row['command']](replay,json.loads(row['raw']))
        equal=build(db)==build(replay)
        return equal
    finally:
        replay.close()

def _journal_coverage(db):
    """Return journal files in order with the evidence ids each one carries."""
    files=[];covered=set()
    for row in db.execute('SELECT id,command,raw FROM file_journal ORDER BY id'):
        data=json.loads(row['raw'])
        ids=[item['id'] for _kind,item in payload_items(row['command'],data)]
        files.append({'journal_id':row['id'],'command':row['command'],'raw':row['raw'],'ids':ids})
        covered.update(ids)
    return files,covered

def _analyze_gap(db):
    """Locate the single missing-file boundary: normalized evidence that has no
    surviving whole file, in evidence insertion order, with surviving anchors."""
    files,covered=_journal_coverage(db)
    evidence=[dict(r) for r in db.execute('SELECT id,kind,sha,raw FROM evidence ORDER BY rowid')]
    order=[r['id'] for r in evidence]
    pos={ident:i for i,ident in enumerate(order)}
    gap_ids=[i for i in order if i not in covered]
    if not gap_ids:return None
    rows={r['id']:r for r in evidence}
    g0,g1=pos[gap_ids[0]],pos[gap_ids[-1]]
    before=[i for i in order[:g0] if i in covered]
    after=[i for i in order[g1+1:] if i in covered]
    anchor_before=rows[before[-1]] if before else None
    anchor_after=rows[after[0]] if after else None
    gap_kinds={rows[i]['kind'] for i in gap_ids}
    command=next(cmd for cmd,sections in SECTION_KIND.items()
                 if gap_kinds <= set(sections.values()))
    pre_files=[f for f in files if f['ids'] and max(pos[i] for i in f['ids'])<g0]
    post_files=[f for f in files if f['ids'] and min(pos[i] for i in f['ids'])>g1]
    order_id=None
    for ident in gap_ids:
        item=json.loads(rows[ident]['raw'])
        if isinstance(item,dict) and 'order_id' in item:order_id=item['order_id'];break
    return {'order_id':order_id,'command':command,'gap_ids':gap_ids,
            'rows':rows,'order':order,'pos':pos,
            'anchor_before':anchor_before,'anchor_after':anchor_after,
            'pre_files':pre_files,'post_files':post_files,
            'surviving_files':len(files)}

def _compositions(n):
    """Every ordered grouping of n sequentially-inserted items into one or more
    submissions: a bit per internal boundary (present/absent)."""
    out=[]
    for mask in range(1<<(n-1) if n>1 else 1):
        groups=[];start=0
        for b in range(n-1):
            if mask>>b&1:groups.append((start,b+1));start=b+1
        groups.append((start,n))
        out.append(groups)
    return out

def _replay_history(db,gap,groups):
    """Independently replay one candidate import history on a fresh database
    using only surviving whole files and surviving normalized items."""
    replay=connect(':memory:')
    try:
        for f in gap['pre_files']:
            HANDLERS[f['command']](replay,json.loads(f['raw']))
        kind_section={kind:sec for sec,kind in SECTION_KIND[gap['command']].items()}
        for lo,hi in groups:
            payload={}
            for ident in gap['gap_ids'][lo:hi]:
                row=gap['rows'][ident]
                payload.setdefault(kind_section[row['kind']],[]).append(json.loads(row['raw']))
            HANDLERS[gap['command']](replay,payload)
        for f in gap['post_files']:
            HANDLERS[f['command']](replay,json.loads(f['raw']))
        oid=gap['order_id']
        recipe_row=replay.execute('SELECT state,reason,recipe_id,version FROM recipe_decision WHERE order_id=?',(oid,)).fetchone()
        shade_row=replay.execute('SELECT state,reason,version FROM shade_release WHERE order_id=?',(oid,)).fetchone()
        lock=replay.execute('SELECT state,shade_version FROM future_recipe_permission WHERE order_id=?',(oid,)).fetchone()
        return {'recipe_state':recipe_row['state'] if recipe_row else None,
                'recipe_reason':recipe_row['reason'] if recipe_row else None,
                'shade_state':shade_row['state'] if shade_row else None,
                'shade_version':shade_row['version'] if shade_row else None,
                'lock':dict(lock) if lock else None}
    finally:
        replay.close()

def _history_vector(gap_ids,groups):
    """Internal-boundary vector: set of relative positions p (1..n-1) where NO
    submission boundary sits between gap items p-1 and p, i.e. they share one
    import. Replays only depend on which items were grouped together."""
    inside=set()
    for lo,hi in groups:
        inside.update(range(lo+1,hi))
    return inside

def _process_witnesses(db,gap,witnesses):
    """Validate both authorized evidence types and turn each into constraints
    over candidate histories. Two valid originals that cannot both hold for the
    same interval are reported as contradictory witnesses — current system
    state or last-entry order is never used to pick a side."""
    reports=[];constraints=[];file_groups=[]
    n=len(gap['gap_ids']);pos=gap['pos'];gap_set=set(gap['gap_ids'])
    for w in witnesses or []:
        if w.get('type')=='handoff_file':
            rep=verify_handoff_file(db,w['command'],w['raw'],w['attested_sha'])
            rep['source']=w.get('source','handoff-archive')
            if rep['valid']:
                ids=[x['id'] for x in rep['items']]
                position=w.get('position') or {}
                if w['command']!=gap['command']:
                    rep['valid']=False;rep['errors'].append('file command does not match the missing-boundary command')
                elif not set(ids)<=gap_set:
                    rep['valid']=False;rep['errors'].append('attested file contains items outside the missing interval')
                elif not position:
                    rep['valid']=False;rep['errors'].append('handoff file lacks the shift-attested submission position; an unattested save-as copy is not admissible')
                else:
                    idx=[pos[i] for i in ids]
                    if idx!=list(range(idx[0],idx[-1]+1)):
                        rep['valid']=False;rep['errors'].append('attested file is not a contiguous segment of the evidence insertion order')
                    ab=gap['anchor_before'];aa=gap['anchor_after']
                    if 'after_anchor' in position and (ab is None or position['after_anchor']!=ab['id']):
                        rep['valid']=False;rep['errors'].append('attested position does not match the surviving before anchor')
                    if 'before_anchor' in position and (aa is None or position['before_anchor']!=aa['id']):
                        rep['valid']=False;rep['errors'].append('attested position does not match the surviving after anchor')
                    if rep['valid']:
                        base=pos[gap['gap_ids'][0]]
                        lo=idx[0]-base
                        grp=[(lo,lo+len(ids))]
                        rep['groups']=[ids]
                        # an attested whole file is one real submission: its
                        # interior has no boundary and both edges are real cuts
                        edge={lo,lo+len(ids)}&set(range(1,n))
                        file_groups.append({'source':rep['source'],'ids':ids,'group':grp})
                        constraints.append({'kind':'same_groups','groups':grp,
                                            'edge_boundaries':edge,'ref':rep['source']})
            reports.append(rep)
        elif w.get('type')=='boundary_manifest':
            rep=verify_boundary_manifest(db,w['manifest'])
            rep['source']=w.get('source','signed-receipt')
            if rep['valid']:
                rel_ids={(r['item_id'],r['other_item_id']) for r in rep['relations']}
                if any(not (a in gap_set and b in gap_set) for a,b in rel_ids):
                    rep['valid']=False;rep['errors'].append('manifest relation references items outside the missing interval')
                ab=gap['anchor_before'];aa=gap['anchor_after']
                for anchor in rep['anchors']:
                    if anchor['role']=='before' and (ab is None or anchor['item_id']!=ab['id']):
                        rep['valid']=False;rep['errors'].append('before anchor is not the surviving predecessor')
                    if anchor['role']=='after' and (aa is None or anchor['item_id']!=aa['id']):
                        rep['valid']=False;rep['errors'].append('after anchor is not the surviving successor')
                if rep['valid']:
                    required=set();forbidden=set()
                    base=pos[gap['gap_ids'][0]]
                    pair_modes={}
                    for r in rep['relations']:
                        pair=tuple(sorted((r['item_id'],r['other_item_id'])))
                        if pair in pair_modes and pair_modes[pair]!=r['mode']:
                            rep['valid']=False
                            rep['errors'].append('manifest declares the same item pair both same-submission and adjacent')
                            break
                        pair_modes[pair]=r['mode']
                        i,j=sorted((pos[r['item_id']],pos[r['other_item_id']]))
                        bi,bj=i-base,j-base
                        if r['mode']=='same':
                            forbidden.update(range(bi+1,bj+1))
                        else:
                            required.add(bi+1)
                    # one signed receipt cannot demand a boundary and forbid it
                    if rep['valid'] and required & forbidden:
                        rep['valid']=False
                        rep['errors'].append('manifest contains mutually incompatible same/adjacent relations')
                    if rep['valid']:
                        constraints.append({'kind':'boundaries','required':required,'forbidden':forbidden,'ref':rep['source']})
            reports.append(rep)
        else:
            reports.append({'kind':'unknown','valid':False,'errors':['unknown witness type: %r'%w.get('type')],'source':w.get('source')})
    contradictions=[]
    for i in range(len(constraints)):
        for j in range(i+1,len(constraints)):
            ci,cj=constraints[i],constraints[j]
            conflict=False
            if ci['kind']=='boundaries' and cj['kind']=='boundaries':
                conflict=bool((ci['required']&cj['forbidden']) or (cj['required']&ci['forbidden']))
            elif ci['kind']=='same_groups' and cj['kind']=='same_groups':
                gi={tuple(g) for g in ci['groups']};gj={tuple(g) for g in cj['groups']}
                if gi==gj:
                    conflict=False  # two attested copies of the same original file
                else:
                    conflict=any(not (set(range(*a)).isdisjoint(set(range(*b)))) for a in gi for b in gj)
            else:
                sg=ci if ci['kind']=='same_groups' else cj
                bd=cj if ci['kind']=='same_groups' else ci
                for lo,hi in sg['groups']:
                    interior=set(range(lo+1,hi))
                    if interior & bd['required']:conflict=True
                if bd['forbidden'] & sg.get('edge_boundaries',set()):conflict=True
                outside_required=set(bd['required'])-sg.get('edge_boundaries',set())
                for lo,hi in sg['groups']:
                    outside_required-=set(range(lo+1,hi))
                if outside_required:conflict=True
            if conflict:contradictions.append({'witness_a':ci['ref'],'witness_b':cj['ref']})
    return reports,constraints,file_groups,contradictions

def _histories_consistent(vector,constraint):
    if constraint['kind']=='boundaries':
        # required: a real submission boundary must exist there -> position absent
        if constraint['required'] & vector:return False
        # forbidden: items must share one submission -> position present
        if not constraint['forbidden'] <= vector:return False
        return True
    required=set()
    for lo,hi in constraint['groups']:
        required.update(range(lo+1,hi))
    if not required <= vector:return False
    # attested file edges are real submission boundaries -> absent from vector
    if constraint.get('edge_boundaries',set()) & vector:return False
    return True

def _booking(recipe_state):
    if recipe_state=='approved':return True
    if recipe_state in ('rework_approval_pending','substitute_pending','unsigned'):return False
    return None

def audit(db,witnesses=None):
    ok=_legacy_replay(db)
    files=db.execute('SELECT count(*) FROM file_journal').fetchone()[0]
    gap=_analyze_gap(db)
    if gap is None:
        return {'ok':ok,'files':files}
    reports,constraints,file_groups,contradictions=_process_witnesses(db,gap,witnesses)
    n=len(gap['gap_ids'])
    histories=[]
    for k,groups in enumerate(_compositions(n),start=1):
        vector=_history_vector(gap['gap_ids'],groups)
        if all(_histories_consistent(vector,c) for c in constraints):
            outcome=_replay_history(db,gap,groups)
            histories.append({'id':'H%d'%k,'groups':[list(gap['gap_ids'][lo:hi]) for lo,hi in groups],
                              'submission_boundaries':[p for p in range(1,n) if p not in vector],'open':True,**outcome})
    contradicted=bool(contradictions)
    if contradicted:
        for h in histories:h['open']=False
    states={h['recipe_state'] for h in histories}
    if contradicted:
        business={'status':'contradicted','field':'recipe_decision.state',
                  'contradictions':contradictions,
                  'booking_allowed':None,
                  'basis':'valid independent originals impose incompatible transaction boundaries'}
    elif not histories:
        business={'status':'unsatisfiable','field':'recipe_decision.state',
                  'booking_allowed':None,
                  'basis':'no import history consistent with all accepted witnesses survives; the witness set itself needs review',
                  'open_histories':[]}
    elif len(states)==1:
        only=histories[0]
        business={'status':'proven','field':'recipe_decision.state','value':only['recipe_state'],
                  'reason':only['recipe_reason'],'booking_allowed':_booking(only['recipe_state']),
                  'basis':'independent replay of every legal recovery history gives the same business decision',
                  'shade_state':only['shade_state'],'open_histories':[h['id'] for h in histories]}
    else:
        business={'status':'indeterminate','field':'recipe_decision.state',
                  'candidate_values':sorted(states),
                  'booking_allowed':None,
                  'basis':'legal recovery histories disagree; current projection and past outputs are not used as proof',
                  'open_histories':[h['id'] for h in histories]}
    attested=[];covered_items=set()
    for fg in file_groups:
        attested.append({'source':fg['source'],'items':fg['ids']})
        covered_items.update(fg['ids'])
    uncovered=[i for i in gap['gap_ids'] if i not in covered_items]
    if contradicted:
        file_status='contradicted'
    elif not uncovered:
        file_status='complete'
    elif attested:
        file_status='partial'
    else:
        file_status='missing'
    file_proof={'status':file_status,'attested_files':attested,'uncovered_items':uncovered,
                'note':None if file_status!='missing' else 'whole-file replay is impossible until original bytes with an attested digest are recovered'}
    evidence_needs=_evidence_needs(gap,histories,business,file_proof)
    return {'ok':ok,'files':files,
            'gap':{'order_id':gap['order_id'],'command':gap['command'],
                   'items':[{'id':i,'kind':gap['rows'][i]['kind'],'sha':gap['rows'][i]['sha']} for i in gap['gap_ids']],
                   'item_insertion_order':list(gap['gap_ids']),
                   'anchors':{'before':None if gap['anchor_before'] is None else {'id':gap['anchor_before']['id'],'sha':gap['anchor_before']['sha']},
                              'after':None if gap['anchor_after'] is None else {'id':gap['anchor_after']['id'],'sha':gap['anchor_after']['sha']}},
                   'surviving_files':gap['surviving_files']},
            'possible_histories':histories,'witnesses':reports,
            'business_proof':business,'file_proof':file_proof,
            'evidence_needs':evidence_needs}

def _evidence_needs(gap,histories,business,file_proof):
    """Minimal next retrieval: one attestable source at a time, targeting only
    the boundary/file bytes still genuinely missing; already-attested items are
    never requested again."""
    needs=[]
    if business['status']=='contradicted':
        disputed=sorted({i for h in histories for grp in h['groups'] for i in grp}) or list(gap['gap_ids'])
        needs.append({'action':'retrieve_handoff_file','target_items':disputed,
                      'reason':'contradictory signed originals; original whole-file bytes with shift-attested digest are the tie-breaker'})
        return needs
    if business['status']=='unsatisfiable':
        needs.append({'action':'retrieve_handoff_file','target_items':list(gap['gap_ids']),
                      'reason':'accepted witnesses admit no consistent history; original whole-file bytes are needed to re-establish the transactions'})
        return needs
    if business['status']=='indeterminate':
        split_seen=None
        for p in range(1,len(gap['gap_ids'])):
            yes={h['recipe_state'] for h in histories if p in h['submission_boundaries']}
            no={h['recipe_state'] for h in histories if p not in h['submission_boundaries']}
            if yes and no and yes!=no:
                split_seen=p;break
        pair=[gap['gap_ids'][split_seen-1],gap['gap_ids'][split_seen]] if split_seen else gap['gap_ids'][:2]
        shas=[gap['rows'][i]['sha'] for i in pair]
        needs.append({'action':'retrieve_boundary_manifest','target_item_shas':shas,
                      'reason':'a signed same/adjacent relation across this boundary decides whether the rework lock was ever set'})
    if file_proof['status'] in ('missing','partial'):
        needs.append({'action':'retrieve_handoff_file','target_items':file_proof['uncovered_items'],
                      'reason':'business proof may hold without it, but the original whole-file journal remains unproven'})
    return needs
