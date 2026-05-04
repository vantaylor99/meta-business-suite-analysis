"""Markdown and summary rendering."""

from __future__ import annotations

from typing import Any


def render_markdown_report(payload: dict[str, Any]) -> str:
    summary = payload["account_summary"]
    benchmarks = payload["benchmarks"]
    measurement_focus = payload.get("measurement_focus") or {}
    lines: list[str] = [
        "# Meta Ads Report",
        "",
    ]
    if payload.get("account_slug"):
        lines.append(f"- Account slug: `{payload['account_slug']}`")
    lines.extend(
        [
        f"- Run date: `{payload['run_date']}`",
        f"- Coverage: `{summary['start_date']}` to `{summary['end_date']}` ({summary['days']} days)",
        f"- Total spend: `${summary['total_spend']:.2f}`",
        f"- Total results: `{_fmt(summary['total_results'])}`",
        f"- Total app installs: `{_fmt(summary['total_app_installs'])}`",
        f"- Purchase value: `${summary['total_purchase_value']:.2f}`",
        f"- Blended ROAS: `{_fmt(benchmarks['account_blended_roas'])}`",
        "",
        "## Executive Summary",
        "",
        (
            f"The account covered {summary['ad_count']} ads across {summary['campaign_count']} campaigns "
            f"and {summary['adset_count']} ad sets. It generated `{_fmt(summary['total_results'])}` recorded results "
            f"and `{_fmt(summary['total_app_installs'])}` app installs on `${summary['total_spend']:.2f}` in spend. "
            f"Blended ROAS was `{_fmt(benchmarks['account_blended_roas'])}`."
        ),
        "",
        ]
    )

    if measurement_focus:
        lines.append("## Measurement Focus")
        lines.append("")
        primary_metric = measurement_focus.get("primary_metric") or "results"
        primary_label = measurement_focus.get("primary_result_label") or "Results"
        lines.append(f"- Primary metric: `{primary_metric}`")
        lines.append(f"- Primary result label: `{primary_label}`")
        if measurement_focus.get("secondary_metric"):
            secondary_label = (
                measurement_focus.get("secondary_metric_label")
                or measurement_focus.get("secondary_metric")
            )
            lines.append(f"- Secondary metric: `{secondary_label}`")
        if measurement_focus.get("roas_role"):
            lines.append(f"- ROAS role: `{measurement_focus['roas_role']}`")
        if measurement_focus.get("analysis_notes"):
            lines.append(f"- Notes: {measurement_focus['analysis_notes']}")
        lines.append("")

    lines.extend(_render_window_summary(payload))
    lines.extend(_render_trajectory_highlights(payload))

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


def _render_window_summary(payload: dict[str, Any]) -> list[str]:
    account_windows = payload.get("account_window_summary") or {}
    if not account_windows:
        return []

    lines = [
        "## Performance By Window",
        "",
        (
            "These windows are sliced from the same daily export and end on "
            f"`{payload['window_comparison_meta']['window_end']}`. Treat the 3-day view as directional."
        ),
        "",
        "| Window | Spend | Results | App installs | Cost / result | Cost / install | Hook rate | Coverage |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for window_key in ("30d", "7d", "3d"):
        window = account_windows.get(window_key)
        if not window:
            continue
        lines.append(
            " | ".join(
                [
                    f"| `{window_key}`",
                    f"`${window['spend']:.2f}`",
                    f"`{_fmt(window['results'])}`",
                    f"`{_fmt(window['app_installs'])}`",
                    f"`{_fmt_currency(window['cost_per_result'])}`",
                    f"`{_fmt_currency(window['cost_per_app_install'])}`",
                    f"`{_fmt_rate(window['hook_rate'])}`",
                    f"`{window['days_with_data']}/{window['requested_days']} days` |",
                ]
            )
        )
    lines.append("")
    return lines


def _render_trajectory_highlights(payload: dict[str, Any]) -> list[str]:
    highlights = payload.get("trajectory_highlights") or []
    lines = [
        "## Trajectory Highlights",
        "",
        (
            "These compare recent efficiency against the longer view. Short windows can be noisy, "
            "so use them to decide what to watch, cap, or retest rather than as standalone scale proof."
        ),
        "",
    ]
    if not highlights:
        lines.append("- No non-insufficient improving or degrading trajectory surfaced from the supplied export.")
        lines.append("")
        return lines

    for item in highlights:
        comparison = "7d vs 30d" if item["comparison"] == "seven_vs_thirty" else "3d vs 7d"
        descriptor = _ad_descriptor(item)
        lines.append(
            f"- {descriptor}: `{item['status']}` on `{comparison}` using `{item['metric']}` "
            f"({_fmt_percent(item['percent_change'])}). {item['reason']}"
        )
    lines.append("")
    return lines


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
                f"spent `${ad['total_spend']:.2f}` with `{_fmt(ad['total_results'])}` results, "
                f"`{_fmt(ad['total_app_installs'])}` app installs, and ROAS `{_fmt(ad['blended_roas'])}` "
                f"and waste score `{ad['waste_score']}`. "
                f"Why it matters: {'; '.join(ad['waste_reasons']) or 'under review'}."
            )
        elif section_kind == "fatigue":
            body = (
                f"fatigue score `{_fmt(ad['fatigue_score'])}` with `{_fmt(ad['total_results'])}` results, "
                f"`{_fmt(ad['total_app_installs'])}` app installs, and ROAS `{_fmt(ad['blended_roas'])}`. "
                f"Signals: {'; '.join(ad['fatigue_reasons']) or 'not enough directional signal'}."
            )
        elif section_kind in {"hook", "weak_hook"}:
            body = (
                f"hook rate `{_fmt(ad['hook_rate'])}`, hold rate `{_fmt(ad['hold_rate'])}`, "
                f"results `{_fmt(ad['total_results'])}`, app installs `{_fmt(ad['total_app_installs'])}`, "
                f"ROAS `{_fmt(ad['blended_roas'])}`."
            )
        elif section_kind == "scaling":
            body = (
                f"scaling score `{_fmt(ad['scaling_score'])}`, spend `${ad['total_spend']:.2f}`, "
                f"results `{_fmt(ad['total_results'])}`, app installs `{_fmt(ad['total_app_installs'])}`, "
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


def _fmt_currency(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"${value:.2f}"


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2%}"


def _fmt_percent(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"
