"""Publish aggregate workflow evidence without prospect names or lead IDs."""
import json
from pathlib import Path


def aggregate_summary(source):
    keys=('mode','run_date_pacific','lead_tier_field_id','downloaded_leads','leads_scanned',
          'changes_proposed','changes_applied','updates_processed_by_tier','review_count')
    result={key:source[key] for key in keys if key in source}
    result['actions']=[{key:action[key] for key in ('action_id','old_tier','new_tier','status','n_leads','n_leads_processed')
                        if key in action} for action in source.get('actions',[])]
    result['guard_skips_to_review']=source.get('guard_skips_to_review',[])
    return result


if __name__=='__main__':
    for path in Path('outputs').glob('lead_tier_sync_*.json'):
        if path.name.endswith('.aggregate.json'):continue
        result=aggregate_summary(json.loads(path.read_text()))
        path.with_suffix('.aggregate.json').write_text(json.dumps(result,indent=2)+'\n')
