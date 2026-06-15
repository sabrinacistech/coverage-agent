from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from architecture import analyzer, rules  # noqa: E402
from architecture.cli import build_parser  # noqa: E402
from architecture.handoff import build_handoff_messages  # noqa: E402
from architecture.repo_sources import (  # noqa: E402
    LocalRepoSource,
    ZipRepoSource,
    classify_path,
    parse_repo_uri,
    resolve_repo_source,
)
from architecture.reporter import render_report, write_outputs  # noqa: E402
from architecture.models import RepoRef, SourceFile  # noqa: E402
import run_architecture_review  # noqa: E402


def test_parse_repo_uri_github_and_enterprise() -> None:
    gh = parse_repo_uri("https://github.com/acme/service.git")
    assert gh.host == "github.com"
    assert gh.owner == "acme"
    assert gh.repo == "service"
    assert gh.api_base == "https://api.github.com"

    ghe = parse_repo_uri("https://github.enterprise.local/org/repo")
    assert ghe.api_base == "https://github.enterprise.local/api/v3"


def test_classify_path() -> None:
    assert classify_path("src/main/java/com/acme/controller/FooController.java") == "java-controller"
    assert classify_path("src/main/resources/application.yml") == "config"
    assert classify_path(".github/workflows/ci.yml") == "ci"
    assert classify_path("Dockerfile") == "docker"


def test_rules_create_structured_findings() -> None:
    finding = rules.controller_repository_coupling(
        "src/main/java/FooController.java",
        "import com.acme.repository.FooRepository;",
    )
    assert finding is not None
    assert finding.id.startswith("arch-")
    assert finding.severity == "HIGH"
    assert finding.source == "architecture-static-rules"
    assert 0 <= finding.confidence <= 1


def test_analyzer_maps_and_findings() -> None:
    files = [
        SourceFile("src/main/java/com/acme/FooController.java", "java-controller", 100),
        SourceFile("src/main/resources/application.properties", "config", 80),
    ]
    contents = {
        files[0].path: """
            package com.acme;
            import com.acme.repository.FooRepository;
            @RestController
            class FooController { FooRepository repo; }
        """,
        files[1].path: "db.password=supersecret",
    }
    arch, dep, findings = analyzer.build_maps(files, contents)
    assert arch["schemaVersion"] == 1
    assert len(arch["components"]["controllers"]) == 1
    assert dep["edge_count"] >= 1
    assert {f.category for f in findings} >= {"layering", "security"}


def test_parser_backed_layer_rules() -> None:
    files = [SourceFile("src/main/java/com/acme/service/FooService.java", "java-service", 100)]
    contents = {
        files[0].path: """
            package com.acme.service;
            import com.acme.controller.FooController;
            @Service
            class FooService { FooController controller; }
        """,
    }
    _, _, findings = analyzer.build_maps(files, contents)
    assert any(f.title == "Service acoplado a capa web" for f in findings)


def test_local_and_zip_sources() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        src = root / "src" / "main" / "java" / "com" / "acme"
        src.mkdir(parents=True)
        (src / "Foo.java").write_text("package com.acme; class Foo {}", encoding="utf-8")

        local = LocalRepoSource(root)
        assert local.repo_ref.host == "local"
        assert local.list_files()[0].path.endswith("Foo.java")
        assert "class Foo" in local.get_content("src/main/java/com/acme/Foo.java")

        zip_path = Path(tmp) / "repo.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(src / "Foo.java", "repo/src/main/java/com/acme/Foo.java")
        zipped = ZipRepoSource(zip_path)
        assert zipped.repo_ref.host == "zip"
        assert zipped.list_files()[0].path == "src/main/java/com/acme/Foo.java"
        assert "class Foo" in zipped.get_content("src/main/java/com/acme/Foo.java")

        resolved = resolve_repo_source(str(root), branch="main", github_api_base=None, token=None)
        assert resolved.repo_ref.host == "local"


def test_reporter_writes_current_outputs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        repo = RepoRef("github.com", "acme", "service", "https://api.github.com")
        files = [SourceFile("src/main/java/Foo.java", "java", 1)]
        arch = {
            "schemaVersion": 1,
            "packages": {"(default)": ["src/main/java/Foo.java"]},
            "components": {
                "controllers": [], "services": [], "repositories": [],
                "entities": [], "dtos": [], "configs": [], "other_java": ["src/main/java/Foo.java"],
            },
            "framework_signals": {},
            "ci_delivery": {"github_actions": [], "dockerfiles": []},
        }
        dep = {"schemaVersion": 1, "edges": [], "edge_count": 0}
        findings = [rules.actuator_not_detectable([])]
        report = render_report(repo, "https://github.com/acme/service", "main", files, arch, dep, findings)
        write_outputs(
            out,
            inventory={
                "schemaVersion": 1,
                "repo_uri": "https://github.com/acme/service",
                "host": "github.com",
                "api_base": "https://api.github.com",
                "owner": "acme",
                "repo": "service",
                "branch": "main",
                "output_dir": str(out),
                "source_type": "github.com",
                "rule": "reports_outside_agent_architecture",
                "files": [],
            },
            architecture_map=arch,
            dependency_map=dep,
            findings=findings,
            report=report,
        )
        assert (out / "source-inventory.json").exists()
        assert (out / "architecture-map.json").exists()
        assert (out / "dependency-map.json").exists()
        data = json.loads((out / "architecture-findings.json").read_text(encoding="utf-8"))
        assert data[0]["source"] == "architecture-static-rules"
        assert (out / "architecture-report.md").read_text(encoding="utf-8").startswith("# Architecture Review")


def test_wrapper_exposes_compatible_parser_args() -> None:
    args = build_parser().parse_args([
        "--repo-uri", "https://github.com/acme/service",
        "--branch", "develop",
        "--out", "state/architecture_app",
        "--github-token-env", "TOKEN",
        "--github-api-base", "https://github.example/api/v3",
        "--max-files", "10",
        "--max-bytes-per-file", "42",
        "--handoff", "none",
    ])
    assert args.repo_uri.endswith("/service")
    assert args.branch == "develop"
    assert args.max_files == 10
    assert args.max_bytes_per_file == 42
    assert args.handoff == "none"
    assert callable(run_architecture_review.main)


def test_handoff_messages_from_outputs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        for name, payload in {
            "source-inventory.json": {"schemaVersion": 1, "files": []},
            "architecture-map.json": {"schemaVersion": 1},
            "dependency-map.json": {"schemaVersion": 1, "edges": []},
            "architecture-findings.json": [],
        }.items():
            (out / name).write_text(json.dumps(payload), encoding="utf-8")
        messages = build_handoff_messages(out)
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "architectureMap" in messages[1]["content"]


def main() -> int:
    tests = [
        test_parse_repo_uri_github_and_enterprise,
        test_classify_path,
        test_rules_create_structured_findings,
        test_analyzer_maps_and_findings,
        test_parser_backed_layer_rules,
        test_local_and_zip_sources,
        test_reporter_writes_current_outputs,
        test_wrapper_exposes_compatible_parser_args,
        test_handoff_messages_from_outputs,
    ]
    for test in tests:
        test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
