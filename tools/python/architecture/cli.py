from __future__ import annotations

import argparse
import os
from dataclasses import asdict

from .analyzer import build_maps
from .handoff import expected_agent_inputs, run_llm_handoff
from .repo_sources import resolve_repo_source, safe_output_dir
from .reporter import render_report, write_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-uri", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--out", default=None)
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    parser.add_argument("--github-api-base", default=None)
    parser.add_argument("--max-files", type=int, default=500)
    parser.add_argument("--max-bytes-per-file", type=int, default=200_000)
    parser.add_argument(
        "--handoff",
        choices=["none", "llm"],
        default="none",
        help="Optional architecture-reviewer handoff via orchestrator.llm_gateway.",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    token = os.environ.get(args.github_token_env)
    source = resolve_repo_source(
        args.repo_uri,
        branch=args.branch,
        github_api_base=args.github_api_base,
        token=token,
    )
    repo_ref = source.repo_ref
    out_dir = safe_output_dir(args.out, repo_ref.repo)

    files = source.list_files()
    files = [f for f in files if f.size <= args.max_bytes_per_file][: args.max_files]

    contents: dict[str, str] = {}
    for item in files:
        try:
            contents[item.path] = source.get_content(item.path)
        except Exception as exc:
            contents[item.path] = f"/* ERROR downloading file: {exc} */"

    inventory = {
        "schemaVersion": 1,
        "repo_uri": args.repo_uri,
        "host": repo_ref.host,
        "api_base": repo_ref.api_base,
        "owner": repo_ref.owner,
        "repo": repo_ref.repo,
        "branch": args.branch,
        "output_dir": str(out_dir),
        "source_type": repo_ref.host,
        "rule": "reports_outside_agent_architecture",
        "files": [asdict(f) for f in files],
        "handoff": expected_agent_inputs(out_dir),
    }
    architecture_map, dependency_map, findings = build_maps(files, contents)
    report = render_report(
        repo_ref, args.repo_uri, args.branch, files, architecture_map, dependency_map, findings
    )
    write_outputs(
        out_dir,
        inventory=inventory,
        architecture_map=architecture_map,
        dependency_map=dependency_map,
        findings=findings,
        report=report,
    )

    if args.handoff == "llm":
        response_path = run_llm_handoff(out_dir)
        print(f"Handoff architecture-reviewer: {response_path}")

    print(f"OK: arquitectura analizada desde {args.repo_uri}@{args.branch}")
    print(f"Host: {repo_ref.host}")
    print(f"API base: {repo_ref.api_base}")
    print(f"Salida externa: {out_dir}")
    print(f"Archivos analizados: {len(files)}")
    print(f"Hallazgos: {len(findings)}")
    print(f"Reporte: {out_dir / 'architecture-report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))
