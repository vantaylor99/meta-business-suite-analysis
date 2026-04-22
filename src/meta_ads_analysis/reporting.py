"""Markdown and summary rendering."""

from __future__ import annotations

from typing import Any


def render_markdown_report(payload: dict[str, Any]) -> str:
    summary = payload["account_summary"]
    benchmarks = payload["benchmarks"]
    lines: list[str] = [
        "# Meta Ads Report",
        "",
        f"- Run date: `{payload['run_date']}`",
        f"- Coverage: `{summary['start_date']}` to `{summary['end_date']}` ({summary['days']} days)",
        f"- Total spend: `${summary['total_spend']:.2f}`",
        f"- Purchase value: `${summary['total_purchase_value']:.2f}`",
        f"- Blended ROAS: `{_fmt(benchmarks['account_blended_roas'])}`",
        "",
        "## Executive Summary",
        "",
        (
            f"The account covered {summary['ad_count']} ads across {summary['campaign_count']} campaigns "
            f"and {summary['adset_count']} ad sets. The current blended ROAS was "
            f"`{_fmt(benchmarks['account_blended_roas'])}` on `${summary['total_spend']:.2f}` in spend."
        ),
        "",
    ]

    lines.extend(_render_ad_section("## Budget Waste Findings", payload["budget_waste"], "waste"))
    lines.extend(_render_ad_section("## Fatigue And Staleness Findings", payload["fatigue_findings"], "fatigue"))

    strong_hooks = payload["hook_findings"]["strong"]
    weak_hooks = payload["hook_findings"]["weak"]
    lines.extend(_render_ad_section("## Hook-Rate And Creative-Performance Findings", strong_hooks, "hook", intro="Strongest hooks"))
    lines.extend(_render_ad_section("", weak_hooks, "weak_hook", intro="Weakest hooks"))

    lines.extend(_render_ad_section("## Scaling Candidates", payload["scaling_candidates"], "scaling"))

    lines.append("## Tracking And Measurement Concerns")
    lines.append("")
    for item in payload["tracking_concerns"]:
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## Recommended Actions For The Next 7 Days")
    lines.append("")
    for item in payload["next_7_day_actions"]:
        lines.append(f"- {item}")
    lines.append("")

    return "\n".join(lines).strip() + "\n"


def _render_ad_section(
    heading: str,
    ads: list[dict[str, Any]],
    section_kind: str,
    intro: str | None = None,
) -> list[str]:
    lines: list[str] = []
    if heading:
        lines.append(heading)
        lines.append("")
    if intro:
        lines.append(f"### {intro}")
        lines.append("")

    if not ads:
        lines.append("- No strong finding surfaced for this section from the supplied export.")
        lines.append("")
        return lines

    for ad in ads:
        descriptor = _ad_descriptor(ad)
        if section_kind == "waste":
            body = (
                f"spent `${ad['total_spend']:.2f}` with ROAS `{_fmt(ad['blended_roas'])}` "
                f"and waste score `{ad['waste_score']}`. "
                f"Why it matters: {'; '.join(ad['waste_reasons']) or 'under review'}."
            )
        elif section_kind == "fatigue":
            body = (
                f"fatigue score `{_fmt(ad['fatigue_score'])}` with ROAS `{_fmt(ad['blended_roas'])}`. "
                f"Signals: {'; '.join(ad['fatigue_reasons']) or 'not enough directional signal'}."
            )
        elif section_kind in {"hook", "weak_hook"}:
            body = (
                f"hook rate `{_fmt(ad['hook_rate'])}`, hold rate `{_fmt(ad['hold_rate'])}`, "
                f"ROAS `{_fmt(ad['blended_roas'])}`."
            )
        elif section_kind == "scaling":
            body = (
                f"scaling score `{_fmt(ad['scaling_score'])}`, spend `${ad['total_spend']:.2f}`, "
                f"ROAS `{_fmt(ad['blended_roas'])}`, fatigue `{_fmt(ad['fatigue_score'])}`."
            )
        else:
            body = "finding available."
        lines.append(f"- {descriptor}: {body}")
    lines.append("")
    return lines


def _ad_descriptor(ad: dict[str, Any]) -> str:
    return f"`{ad['ad_name']}` in `{ad['campaign_name']}` / `{ad['adset_name']}`"


def _fmt(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
