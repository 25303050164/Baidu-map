"""Allowlisted collection exports; spreadsheet formulas are emitted as text."""
import csv
import io
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from app.persistence import atomic_dump
from app.request_control import RequestStopped
from .models import CATEGORIES
from .planner import digest


def csv_text(rows):
    fields = ['id', 'name', 'category', 'classificationStatus', 'lng', 'lat', 'address',
              'possibleDuplicateGroup', 'classificationRuleVersion', 'classificationEvidence', 'provenance', 'reviewPurpose']
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fields)
    writer.writeheader()
    for item in rows:
        row = {k: item.get(k, '') for k in fields}
        row.update(item['location'])
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                value = "'" + value
            row[key] = value
        writer.writerow(row)
    return stream.getvalue()


def export_artifacts(output, config, plan, result, runtime, *, secret=''):
    output = Path(output)
    payload = result.model_dump(mode='json', by_alias=True)
    review = [{**p, 'reviewPurpose': 'classification'} for p in payload['reviewCandidates']]
    for category in CATEGORIES:
        candidates = sorted((p for p in payload['pois'] if p['category'] == category), key=lambda p: digest(p['id']))
        review.extend({**p, 'reviewPurpose': 'accepted_sample'} for p in candidates[:15])
    sampled = {p['id'] for p in review}
    review.extend({**p, 'reviewPurpose': 'possible_duplicate'} for p in payload['pois']
                  if p['possibleDuplicateGroup'] and p['id'] not in sampled)
    ledger = runtime.ledger.data if runtime.ledger else {'schemaVersion': 'poi-ledger-v1', 'runId': runtime.config.run_id,
        'source': 'synthetic', 'status': result.query_status, 'events': runtime.events, 'counts': runtime.counts}
    metrics = {**payload['statistics'], 'classificationRuleVersion': plan['ruleVersion'],
        'querySequences': len(payload['queryCoverage']),
        'completedSequences': sum(q['status'] == 'completed' for q in payload['queryCoverage']),
        'sampleMethod': 'sort-by-sha256-id-first-15-per-category-v1', 'manualReviewCompleted': False}
    metrics['coverageByCategory'] = {c: {
        'planned': sum(q['category'] == c for q in payload['queryCoverage']),
        **{status: sum(q['category'] == c and q['status'] == status for q in payload['queryCoverage'])
           for status in ('completed', 'partial', 'failed')}
    } for c in CATEGORIES}
    objects = {'config-sanitized.json': config.model_dump(mode='json', by_alias=True), 'query-plan.json': plan,
        'poi-result.json': payload, 'query-coverage.json': payload['queryCoverage'], 'metrics.json': metrics,
        'ledger-final.json': ledger}
    files = {name: json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + '\n'
             for name, value in objects.items()}
    files['poi-list.csv'] = csv_text(payload['pois'])
    files['review.csv'] = csv_text(review)
    files['验收报告.md'] = f'''# POI B1 采集结果

- 运行：{result.collection_id}；数据来源：{result.data_source}；查询状态：{result.query_status}。
- 分类数量：{json.dumps(payload['countsByCategory'], ensure_ascii=False)}。
- 查询完成：{metrics['completedSequences']}/{metrics['querySequences']}；待核 {len(result.review_candidates)} 条。
- 网络预留 {metrics['networkReservations']} 次；已收到响应证明发送 {metrics['confirmedSent']} 次；无法确认的保守占用 {metrics['conservativeReservations']} 次。
- 现实设施目录完整性：unverified；分类抽查、已知设施命中率和现场核对均未执行。
- 查询限制：{', '.join(result.warnings) or '无查询异常；仍不保证现实目录完整'}。

本结果为{'离线合成回放，仅验证工程能力，不能当作真实社区设施' if not runtime.live else '有限范围检索，需要人工质量核验'}。
坐标为 BD09LL，展示点与导航点分开保存；没有验证入口、营业状态、15 分钟可达性或盲区面积。
review.csv 按预先固定的哈希排序抽样，另列分类待核与疑似重复，尚未填写人工结论。
ledger-final.json 是审计快照，不可作为新额度或续跑来源；在线权威账本保存在后端 .poi-ledgers 下。
'''
    # Scan in memory before publishing any artifact; do not persist sensitive diagnostics.
    if any((secret and secret in value) or re.search(r'(?i)(?:[?&]|\b)(?:ak|password|token)=[^\s]+', value)
           for value in files.values()):
        raise RequestStopped('sensitive_output')
    if output.exists():
        raise RequestStopped('output_already_exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Publish a complete directory on the same filesystem only after every write succeeds.
    with TemporaryDirectory(prefix='.poi-export-', dir=output.parent) as staging_root:
        staging = Path(staging_root) / 'result'
        staging.mkdir()
        for name, value in files.items():
            (staging / name).write_text(value, encoding='utf-8', newline='')
        atomic_dump(staging / 'security-check.json', {'passed': True, 'filesChecked': sorted(files),
            'exactCredentialChecked': bool(secret), 'rawTransportCaptured': False, 'csvFormulaEscaping': True})
        if output.exists():
            raise RequestStopped('output_already_exists')
        staging.rename(output)
