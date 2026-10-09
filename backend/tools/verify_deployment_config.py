"""B1: read what this deployment is actually configured to do, without doing it.

``evidence/05`` has to state the effective tier, the ceilings and the day's balance,
and every earlier attempt at that was a throwaway script that left no re-runnable
evidence. This tool is that check, kept:

* it installs the external-connection guard first, so a mistake here fails loudly
  instead of spending a real account's allowance;
* it **never** reads the AK's value — only whether one is configured;
* it opens the ledger with a read-only URI, so ``DailyLedger.initialize()`` never
  runs: reporting the balance must not create the database it reports on;
* it prints no environment *values* at all, only key names, so its output is safe to
  paste into an evidence file.

Two things it is deliberately opinionated about, because both have already gone wrong
once:

* **A key nothing reads.** ``Settings`` ignores unknown environment names, so a
  misspelled key (``ANALYSIS_PROVIDE``) is not an error anywhere — the setting just
  silently keeps its default. Every ``.env`` name that matches no field is reported.
* **A ceiling above the account.** The application's own bound is not headroom: a QPS
  above what the console grants can only fail upstream. The report compares the
  configured ceilings with ``config.VERIFIED_ENTITLEMENT``.

Run from ``backend/``: ``.venv/bin/python -m tools.verify_deployment_config``.
Exit status is 2 when something needs an operator's attention, so a caller can gate on
it; the findings are in the JSON either way.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from tools.poi_benchmark_guard import Guard, install

#: ``Settings`` field names as environment names, plus the few names pydantic-settings
#: itself understands. Anything else in ``.env`` is read by nobody.
SETTINGS_PREFIXES = ('_ENV_FILE',)


def parse_env_names(text: str) -> list[str]:
    """The ``NAME=`` keys of an env file, in file order, with nothing of the values."""
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        name = stripped.split('=', 1)[0].strip()
        if name.startswith('export '):
            name = name[len('export '):].strip()
        if name:
            names.append(name)
    return names


def ignored_names(names, fields) -> list[str]:
    """The names nothing reads. The reason this tool exists rather than a comment."""
    known = {field.upper() for field in fields}
    return [name for name in names if name not in known and not name.startswith(SETTINGS_PREFIXES)]


def ledger_rows(path: Path):
    """Every ``daily_spend`` row, read-only. Never creates the file it reads."""
    if not path.is_file():
        return None, 'missing'
    try:
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    except sqlite3.Error as error:
        return None, f'unreadable: {error}'
    try:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(
            'SELECT service, day, spent, updated_at FROM daily_spend ORDER BY day, service')]
    except sqlite3.Error as error:
        return None, f'unreadable: {error}'
    finally:
        connection.close()
    return rows, None


def store_files(root: Path) -> dict:
    if not root.is_dir():
        return {'exists': False, 'files': 0, 'bytes': 0}
    files = [item for item in root.rglob('*') if item.is_file()]
    return {'exists': True, 'files': len(files),
            'bytes': sum(item.stat().st_size for item in files)}


def inspect() -> dict:
    from app.config import BACKEND_DIR, VERIFIED_ENTITLEMENT, Settings
    from app.quota import DIRECTION, PLACE, SHANGHAI, QuotaTier, ServiceTier

    env_path = BACKEND_DIR / '.env'
    settings = Settings()
    tier = QuotaTier(
        ServiceTier(settings.baidu_direction_qps, settings.baidu_place_qps,
                    settings.baidu_place_daily_budget, 'current'),
        ServiceTier(settings.baidu_fallback_direction_qps, settings.baidu_fallback_place_qps,
                    settings.baidu_fallback_place_daily_budget, 'fallback'),
        settings.baidu_quota_fallback_at)
    active = tier.active()
    rows, ledger_error = ledger_rows(Path(settings.quota_ledger_path))
    today = datetime.now(SHANGHAI).date().isoformat()
    today_rows = [] if rows is None else [row for row in rows if row['day'] == today]

    names = parse_env_names(env_path.read_text('utf-8')) if env_path.is_file() else []
    ceilings = {'place': active.place_qps, 'walking': active.direction_qps}
    entitlement = {'place': VERIFIED_ENTITLEMENT['placeQps'],
                   'walking': VERIFIED_ENTITLEMENT['walkingQps']}
    over = {name: value for name, value in ceilings.items() if value > entitlement[name]}
    return {
        'envFile': {'present': env_path.is_file(),
                    'ignoredNames': ignored_names(names, type(settings).model_fields)},
        'provider': {'analysisProvider': settings.analysis_provider,
                     'akConfigured': settings.ak_configured},
        'tier': {'active': active.label, 'switchAt': settings.baidu_quota_fallback_at.isoformat(),
                 'current': {'walkingQps': tier.current.direction_qps,
                             'placeQps': tier.current.place_qps,
                             'placeDailyBudget': tier.current.place_daily_budget},
                 'fallback': {'walkingQps': tier.fallback.direction_qps,
                              'placeQps': tier.fallback.place_qps,
                              'placeDailyBudget': tier.fallback.place_daily_budget}},
        'entitlement': {**VERIFIED_ENTITLEMENT, 'configuredAbove': over},
        'matrixEnabled': settings.baidu_matrix_enabled,
        'cache': {'freshnessSeconds': settings.cache_freshness_seconds,
                  'crossTaskReuse': settings.cache_freshness_seconds is not None},
        'ledger': {'path': str(settings.quota_ledger_path), 'error': ledger_error,
                   'today': today, 'todayRows': today_rows,
                   'placeHistory': [] if rows is None else
                   [row for row in rows if row['service'] == PLACE]},
        'store': {'path': str(settings.checkup_dir), **store_files(Path(settings.checkup_dir))},
        'services': {'place': PLACE, 'direction': DIRECTION},
    }


def findings(report: dict) -> list[str]:
    problems = []
    for name in report['envFile']['ignoredNames']:
        problems.append(f'.env 里 {name} 没有被任何设置项读取（值静默失效）')
    for name, value in report['entitlement']['configuredAbove'].items():
        problems.append(f'生效的 {name} QPS {value} 高于控制台核实的账号权益')
    if report['ledger']['error'] is not None:
        problems.append(f"账本不可读：{report['ledger']['error']}")
    if not report['provider']['akConfigured']:
        problems.append('未配置 AK，真实检索不可用（离线用途可忽略）')
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--json', action='store_true', help='只输出 JSON')
    args = parser.parse_args(argv)

    guard = install(Guard())
    report = inspect()
    report['externalConnectionAttempts'] = guard.report()['attempts']
    problems = findings(report)
    report['findings'] = problems

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 2 if problems else 0

    tier = report['tier']
    print(f"生效档位：{tier['active']}（切换点 {tier['switchAt']}）")
    for label in ('current', 'fallback'):
        values = tier[label]
        print(f"  {label:8s} 步行 {values['walkingQps']} QPS，地点 {values['placeQps']} QPS，"
              f"应用日预算 {values['placeDailyBudget']}")
    print(f"账号权益（控制台 2026-10-09）：地点 {report['entitlement']['placeQps']} QPS/"
          f"{report['entitlement']['placeDailyCalls']} 次每天，步行 "
          f"{report['entitlement']['walkingQps']} QPS/{report['entitlement']['walkingDailyCalls']} 次每天")
    print(f"provider：{report['provider']['analysisProvider']}；"
          f"AK 已配置：{report['provider']['akConfigured']}（值未读取）")
    print(f"账本：{report['ledger']['path']}"
          + ('' if report['ledger']['error'] is None else f"（{report['ledger']['error']}）"))
    print(f"  今天（{report['ledger']['today']}）：{report['ledger']['todayRows'] or '无记录'}")
    print(f"  place 历史：{[row['day'] + '=' + str(row['spent']) for row in report['ledger']['placeHistory']]}")
    print(f"体检存储：{report['store']['path']} —— {report['store']['files']} 个文件，"
          f"{report['store']['bytes'] / 1048576:.1f} MB")
    print(f"外连尝试：{report['externalConnectionAttempts']}（必须为 0）")
    print()
    if problems:
        print('需要注意：')
        for item in problems:
            print(f'  - {item}')
    else:
        print('未发现问题。')
    return 2 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
