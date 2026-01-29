"""
Celery Tasks for Multi-Agent Monitoring Pipeline
Scheduled tasks for detecting, validating, and tracking issues.
Includes Teams and email alerts for critical events.
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime

from celery_app import celery_app
from config import logger as app_logger

logger = get_task_logger(__name__)


def send_alerts_if_needed(health: dict, regressions: list, critical_issues: list = None):
    """Helper to send alerts based on monitoring results"""
    try:
        from services.alerts_service import (
            alert_critical_issues, alert_health_drop, alert_regressions,
            get_health_threshold, is_alerts_enabled
        )

        if not is_alerts_enabled():
            return

        # Alert on critical issues
        if critical_issues:
            alert_critical_issues(critical_issues)

        # Alert on health drop
        threshold = get_health_threshold()
        if health.get('health_score', 100) < threshold:
            alert_health_drop(health['health_score'], details=health)

        # Alert on regressions
        if regressions:
            alert_regressions(regressions)

    except Exception as e:
        logger.error(f"Failed to send alerts: {e}")


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=300,
    acks_late=True,
)
def run_monitoring_pipeline(self, hours: int = 24, use_ai: bool = True, save_snapshot: bool = False):
    """
    Run the complete monitoring pipeline (Agent 1 + 2 + 3).

    Recommended schedule: Every 6 hours for detection, daily with snapshot.

    Args:
        hours: Hours of history to analyze
        use_ai: Whether to use AI validation in Agent 2
        save_snapshot: Whether to save daily health snapshot
    """
    try:
        logger.info(f"Starting monitoring pipeline: hours={hours}, use_ai={use_ai}, snapshot={save_snapshot}")

        from agents.interaction_monitor import analyze_interactions
        from agents.issue_validator import validate_issues
        from agents.resolution_tracker import calculate_health_metrics, save_health_snapshot, detect_regressions, auto_resolve_stale_issues

        results = {
            'started_at': datetime.utcnow().isoformat(),
            'agent1': None,
            'agent2': None,
            'agent3': None,
        }

        # Agent 1: Interaction Monitor
        logger.info("Running Agent 1: Interaction Monitor")
        monitor_results = analyze_interactions(hours=hours, dry_run=False)
        results['agent1'] = {
            'logs_analyzed': monitor_results['logs_analyzed'],
            'issues_found': len(monitor_results['issues_found']),
            'summary': monitor_results.get('summary', {})
        }
        logger.info(f"Agent 1 complete: {results['agent1']['issues_found']} issues detected")

        # Agent 2: Issue Validator (if there are issues to validate)
        if results['agent1']['issues_found'] > 0:
            logger.info("Running Agent 2: Issue Validator")
            validator_results = validate_issues(limit=100, use_ai=use_ai, dry_run=False)
            results['agent2'] = {
                'processed': validator_results['issues_processed'],
                'validated': len(validator_results['validated']),
                'false_positives': len(validator_results['false_positives']),
                'patterns': validator_results.get('patterns_found', {})
            }
            logger.info(f"Agent 2 complete: {results['agent2']['validated']} validated, {results['agent2']['false_positives']} false positives")
        else:
            logger.info("Agent 2 skipped: no new issues to validate")

        # Agent 3: Resolution Tracker
        logger.info("Running Agent 3: Resolution Tracker")
        health = calculate_health_metrics(days=7)
        regressions = detect_regressions()
        auto_resolved = auto_resolve_stale_issues()

        results['agent3'] = {
            'health_score': health['health_score'],
            'health_status': health['health_status'],
            'issue_rate': health['issue_rate'],
            'open_issues': health['open_issues'],
            'regressions': len(regressions),
            'auto_resolved': len(auto_resolved)
        }

        if save_snapshot:
            save_health_snapshot(health)
            logger.info("Daily health snapshot saved")

        logger.info(f"Agent 3 complete: health={health['health_score']:.0f}/100 ({health['health_status']})")

        if auto_resolved:
            logger.info(f"Agent 3 auto-resolved {len(auto_resolved)} stale issues")

        # Log summary
        results['completed_at'] = datetime.utcnow().isoformat()
        logger.info(f"Monitoring pipeline complete: {results}")

        # Alert on critical health
        if health['health_score'] < 70:
            logger.warning(f"CRITICAL: Health score is {health['health_score']:.0f}/100")

        if regressions:
            logger.warning(f"REGRESSION: {len(regressions)} pattern(s) have recurred after being fixed")

        # Send alerts (Teams + Email)
        send_alerts_if_needed(health, regressions)

        return results

    except Exception as exc:
        logger.exception("Error in monitoring pipeline")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
)
def run_interaction_monitor(self, hours: int = 6):
    """
    Run Agent 1 only: Detect anomalies in recent interactions.

    Lightweight task for frequent checks (every 2-4 hours).
    """
    try:
        logger.info(f"Running interaction monitor: hours={hours}")

        from agents.interaction_monitor import analyze_interactions

        results = analyze_interactions(hours=hours, dry_run=False)

        logger.info(f"Interaction monitor complete: {len(results['issues_found'])} issues in {results['logs_analyzed']} logs")

        return {
            'logs_analyzed': results['logs_analyzed'],
            'issues_found': len(results['issues_found']),
            'summary': results.get('summary', {})
        }

    except Exception as exc:
        logger.exception("Error in interaction monitor")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
)
def run_issue_validator(self, limit: int = 50, use_ai: bool = False):
    """
    Run Agent 2 only: Validate pending issues.

    Can be run frequently with use_ai=False for rule-based validation,
    or less frequently with use_ai=True for deeper analysis.
    """
    try:
        logger.info(f"Running issue validator: limit={limit}, use_ai={use_ai}")

        from agents.issue_validator import validate_issues

        results = validate_issues(limit=limit, use_ai=use_ai, dry_run=False)

        logger.info(f"Issue validator complete: {len(results['validated'])} validated, {len(results['false_positives'])} false positives")

        return {
            'processed': results['issues_processed'],
            'validated': len(results['validated']),
            'false_positives': len(results['false_positives']),
            'patterns': results.get('patterns_found', {})
        }

    except Exception as exc:
        logger.exception("Error in issue validator")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=1,
    acks_late=True,
)
def save_daily_health_snapshot(self):
    """
    Save daily health snapshot for trend tracking.

    Run once per day, ideally at a consistent time (e.g., midnight UTC).
    """
    try:
        logger.info("Saving daily health snapshot")

        from agents.resolution_tracker import calculate_health_metrics, save_health_snapshot

        metrics = calculate_health_metrics(days=1)
        save_health_snapshot(metrics)

        logger.info(f"Health snapshot saved: score={metrics['health_score']:.0f}/100")

        return {
            'health_score': metrics['health_score'],
            'health_status': metrics['health_status'],
            'issue_rate': metrics['issue_rate']
        }

    except Exception as exc:
        logger.exception("Error saving health snapshot")
        raise


@celery_app.task(
    bind=True,
    max_retries=1,
    acks_late=True,
)
def generate_weekly_health_report(self):
    """
    Generate and send weekly health report via Teams and Email.

    Run once per week (e.g., Monday morning).
    """
    try:
        logger.info("Generating weekly health report")

        from agents.resolution_tracker import generate_weekly_report, format_report
        from services.alerts_service import send_weekly_report_alert, is_alerts_enabled

        report = generate_weekly_report()
        formatted = format_report(report)

        # Log the full report
        logger.info(f"Weekly health report:\n{formatted}")

        # Log key metrics at WARNING level for visibility
        health = report['current_health']
        if health['health_score'] < 80:
            logger.warning(f"Weekly health score: {health['health_score']:.0f}/100 ({health['health_status']})")
        else:
            logger.info(f"Weekly health score: {health['health_score']:.0f}/100 ({health['health_status']})")

        if report.get('regressions'):
            logger.warning(f"Regressions detected: {len(report['regressions'])} pattern(s)")

        if report.get('recommendations'):
            high_priority = [r for r in report['recommendations'] if r['priority'] == 'high']
            if high_priority:
                logger.warning(f"High priority recommendations: {len(high_priority)}")
                for rec in high_priority:
                    logger.warning(f"  - {rec['title']}: {rec['action']}")

        # Send weekly report via Teams and Email
        if is_alerts_enabled():
            send_weekly_report_alert(report)
            logger.info("Weekly report sent via Teams and Email")

        return {
            'health_score': health['health_score'],
            'health_status': health['health_status'],
            'recommendations_count': len(report.get('recommendations', [])),
            'regressions_count': len(report.get('regressions', []))
        }

    except Exception as exc:
        logger.exception("Error generating weekly report")
        raise


@celery_app.task(
    bind=True,
    max_retries=1,
    acks_late=True,
)
def check_critical_issues(self):
    """
    Quick check for critical/high severity open issues.

    Run frequently (every hour) to catch urgent problems fast.
    Logs warnings and sends alerts for critical issues.
    """
    try:
        from agents.resolution_tracker import get_open_issues
        from services.alerts_service import alert_critical_issues, is_alerts_enabled

        issues = get_open_issues(limit=20)

        critical = [i for i in issues if i['severity'] == 'critical']
        high = [i for i in issues if i['severity'] == 'high']

        if critical:
            logger.warning(f"CRITICAL ISSUES: {len(critical)} unresolved")
            for issue in critical:
                logger.warning(f"  - #{issue['id']} {issue['issue_type']}: {issue.get('details', {}).get('user_message', '')[:50]}")

            # Send alerts for critical issues
            if is_alerts_enabled():
                alert_critical_issues(critical)

        if high:
            logger.warning(f"HIGH PRIORITY ISSUES: {len(high)} unresolved")

        return {
            'total_open': len(issues),
            'critical': len(critical),
            'high': len(high)
        }

    except Exception as exc:
        logger.exception("Error checking critical issues")
        raise
