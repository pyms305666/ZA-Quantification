"""Freeze acceptance gates once; do not use the already seen first batch."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/研究/2026-09-28-开盘观察'


def main():
    path=OUT/'接入判据.json'
    if path.exists():raise RuntimeError('Qualification rule is already frozen')
    now=int(time.time()*1000)
    value=dict(version=1,created_at=datetime.fromtimestamp(now/1000,timezone(timedelta(hours=8))).isoformat(),
        qualification_begins_after_ms=now,exclude_first_batch='2026-09-28 09:15 predictions and 09:30 outcomes were already seen',
        endpoint='15m price direction from saved quote anchor to first fresh quote within 5s after due',
        costs='10 yuan/lot roundtrip + adverse 1 tick per side; terminal quote diagnostic, not executable fill',
        minimum_days=60,minimum_paired_15m_endpoints_per_day=32,minimum_candidate_signals=300,
        minimum_each_direction=80,minimum_coverage=.20,max_coverage_drop=.10,minimum_accuracy_lift=.05,
        minimum_products=6,minimum_signals_per_product=30,minimum_positive_months=3,
        minimum_endpoint_completeness=.95,day_block_bootstrap_draws=2000,
        required_extra_reviews=['matched realistic execution accounting','drawdown and loss nonworsening','desktop and Android integration parity'],
        no_automatic_replacement=True,
        interpretation='These are minimum research promotion gates, not a promise of no future losses. Source and model versions remain frozen during observation.')
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(value,ensure_ascii=False))


if __name__=='__main__':main()
