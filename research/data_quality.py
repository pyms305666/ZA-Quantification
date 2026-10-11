"""Coverage checks shared by collection and modelling."""
from datetime import datetime
from market.decision_profiles import CN


def truncated_prefix(payload):
    short=payload['bars']['900']
    if not short or not payload['coverage']['900']['hit_limit']:return False
    start=int(datetime.fromisoformat(payload['start']).replace(tzinfo=CN).timestamp()*1000)
    first=next((b[0] for b in payload['bars']['86400'] if b[0]>=start and b[4]>0 and b[5]>0),None)
    return first is not None and short[0][0]>first+15*3600000
