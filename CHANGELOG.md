## [1.2.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.2.1...v1.2.2) (2026-05-29)


### Bug Fixes

* **eval-analyze:** replace {{ stdout }} with {{ conversation }} in template ([#81](https://github.com/opendatahub-io/agent-eval-harness/issues/81)) ([50ab3cb](https://github.com/opendatahub-io/agent-eval-harness/commit/50ab3cb1e6bf2877ccbdc98501793a4454f8b466))

## [1.2.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.2.0...v1.2.1) (2026-05-29)


### Bug Fixes

* **eval-run:** rename --no-judge, --case-filter, exact case matching ([#80](https://github.com/opendatahub-io/agent-eval-harness/issues/80)) ([06a3d0c](https://github.com/opendatahub-io/agent-eval-harness/commit/06a3d0cf43190099ae611c8481c5ef048a4068c5))

# [1.2.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.1.0...v1.2.0) (2026-05-29)


### Features

* **eval-analyze:** update skills for builtin judges and add list_builtins script ([#79](https://github.com/opendatahub-io/agent-eval-harness/issues/79)) ([c2aff38](https://github.com/opendatahub-io/agent-eval-harness/commit/c2aff380716da6054ea406edb8678642ca70f0a3))

# [1.1.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.0.0...v1.1.0) (2026-05-29)


### Features

* add reusable judges library with builtin registry ([#66](https://github.com/opendatahub-io/agent-eval-harness/issues/66)) ([1e51b41](https://github.com/opendatahub-io/agent-eval-harness/commit/1e51b411392bda8fd3d404733f21ad2b62aaa55b))

# 1.0.0 (2026-05-27)


### Bug Fixes

* address CodeRabbit findings in ensure_deps.py ([f17cd72](https://github.com/opendatahub-io/agent-eval-harness/commit/f17cd72701763daf5c08290ba23b5c30074bdb07))
* address CodeRabbit findings on PR [#25](https://github.com/opendatahub-io/agent-eval-harness/issues/25) ([4d4064f](https://github.com/opendatahub-io/agent-eval-harness/commit/4d4064fa5d4e6ad8bd91bc7d2b25142326bb4fe4))
* address CodeRabbit review feedback on CLI runner PR ([2d90738](https://github.com/opendatahub-io/agent-eval-harness/commit/2d90738e7b9d0ed097a8fc5f9422179161642ea7))
* address CodeRabbit review feedback on EvalHub PR ([b86f754](https://github.com/opendatahub-io/agent-eval-harness/commit/b86f754c54c65b403ac1f51d370d4360c2a0ffdd))
* address CodeRabbit review feedback on release pipeline ([7938e61](https://github.com/opendatahub-io/agent-eval-harness/commit/7938e61ba36728b798904ea19c912c7b86915ce3))
* address CodeRabbit review findings ([0719088](https://github.com/opendatahub-io/agent-eval-harness/commit/071908887386ab2c3ebc9f5799269215a6dc10a3))
* address CodeRabbit review findings on report.py ([0992b80](https://github.com/opendatahub-io/agent-eval-harness/commit/0992b80961139550268f5a634667b3da05c43ac5))
* address eval-analyze skill review findings ([183e606](https://github.com/opendatahub-io/agent-eval-harness/commit/183e606ac4dca0a03b72067f1681900f9bfea1bd))
* address remaining CodeRabbit review items on CLI runner ([94cbcb1](https://github.com/opendatahub-io/agent-eval-harness/commit/94cbcb1c32d366725da71b97e1323ffdf7946c17))
* apply Rui's EvalHub provider registration corrections ([628a85e](https://github.com/opendatahub-io/agent-eval-harness/commit/628a85e90751c2b5ea9bd608bf0d3e6f1063837a))
* bootstrap pyyaml before parsing eval.yaml in ensure_deps ([aec5acf](https://github.com/opendatahub-io/agent-eval-harness/commit/aec5acf024fc8dae3f543f05389989a20e9e728c))
* bump plugin.json and marketplace.json versions during release ([0b73d63](https://github.com/opendatahub-io/agent-eval-harness/commit/0b73d6330f4c97c1addbc52c536a8a6b8adf33ae))
* **ci:** bump Node.js to 22 for semantic-release ([0b409ff](https://github.com/opendatahub-io/agent-eval-harness/commit/0b409ff4bc982a969aea1e34f3d6013c64fb7c71))
* default model examples to claude-opus-4-6 for skill/judge, sonnet for hook ([5fac006](https://github.com/opendatahub-io/agent-eval-harness/commit/5fac006847fa2c58cd961cc0a4a25d93b01ed7d4))
* detect and surface permission denials during eval-run execution ([ba9b9a0](https://github.com/opendatahub-io/agent-eval-harness/commit/ba9b9a0c4e6d9c2712d62b4153c9c38860386136)), closes [#34](https://github.com/opendatahub-io/agent-eval-harness/issues/34)
* disable persist-credentials in tests.yml checkout ([b5ab1ec](https://github.com/opendatahub-io/agent-eval-harness/commit/b5ab1ecb9decf0a74c734cb89f5df63e9e134d5a))
* handle multiple MLflow runs per eval_run_id in from_traces.py ([b6cf4ef](https://github.com/opendatahub-io/agent-eval-harness/commit/b6cf4efa9f1e1c48066c7c013f46ec8048d16daf))
* improve execution mode detection in eval-analyze ([42b62a2](https://github.com/opendatahub-io/agent-eval-harness/commit/42b62a2d1d7d0887ea199bcd9604bd2a48b8ac27))
* improve report badge rendering for regression and markdown tables ([2a55174](https://github.com/opendatahub-io/agent-eval-harness/commit/2a551747e7094053b994ec6304735122033aab61))
* initialize git repos in eval workspaces for settings discovery ([801a255](https://github.com/opendatahub-io/agent-eval-harness/commit/801a255f17a19ac86c89b3f97ce91fdbcfe5e83c))
* merge eval.yaml permissions.allow into workspace settings.json ([8810451](https://github.com/opendatahub-io/agent-eval-harness/commit/88104519df8a926be77f71a037edad6fe9b6c45c))
* remove beads data, gitignore .beads/, fix plugin order ([9fbc1c7](https://github.com/opendatahub-io/agent-eval-harness/commit/9fbc1c736faec85084724679bbd0e3968ff50d48))
* remove unused RunnerConfig.plugins field ([7177d73](https://github.com/opendatahub-io/agent-eval-harness/commit/7177d73e0481a6d46823ee9b2252471a68505e45))
* resolve merge conflict with main in report.py ([1da05f8](https://github.com/opendatahub-io/agent-eval-harness/commit/1da05f81bbd39e82570c2812c4ec799eb0ebf370))
* revert dev marketplace.json to local source reference ([f7077b3](https://github.com/opendatahub-io/agent-eval-harness/commit/f7077b380d8b7d810282cfd49a8d8e71752895cf))
* **score:** restore stdout loading and add batch-mode fallbacks ([1634d6d](https://github.com/opendatahub-io/agent-eval-harness/commit/1634d6d2a35b98fa7c01454918e863f4965c7888))
* tighten permission-denial matcher and e2e assertion ([b4d3852](https://github.com/opendatahub-io/agent-eval-harness/commit/b4d3852a7712f76a9830dcfe1ad0fec9341845ba))
* update remaining 4-7 model IDs to 4-6 in eval.yaml ([3c8b1a8](https://github.com/opendatahub-io/agent-eval-harness/commit/3c8b1a8b042cc6de772fce097ffe120256ae8d1b))
* use GitHub source reference in dev marketplace.json ([c61eb04](https://github.com/opendatahub-io/agent-eval-harness/commit/c61eb04a6a00c3aff35c661479acc4e2c3e4baf5))
* use jq for JSON version bumps instead of sed ([e2ee502](https://github.com/opendatahub-io/agent-eval-harness/commit/e2ee502d7f43c8f23fd990e5b679a24ea2208f83))
* validate thresholds is a mapping before iterating ([a9e8aa5](https://github.com/opendatahub-io/agent-eval-harness/commit/a9e8aa5ee81159834878380bf4a2c95df72c40cc))


### Features

* add [EXTERNAL] convention for external-state fields in dataset schema ([b44c268](https://github.com/opendatahub-io/agent-eval-harness/commit/b44c26841426a973dfe15e2bbe48ddb55a612ceb)), closes [#34](https://github.com/opendatahub-io/agent-eval-harness/issues/34)
* add opaque CLI runner for arbitrary agent commands ([6879853](https://github.com/opendatahub-io/agent-eval-harness/commit/68798536d3552466367c37662277f81f09cb1468))
* add parallel case execution for eval-run ([b920489](https://github.com/opendatahub-io/agent-eval-harness/commit/b920489b2fd64ae6fc32cd9e435e6254eefe693b))
* add semantic-release pipeline for automated versioning ([c05128e](https://github.com/opendatahub-io/agent-eval-harness/commit/c05128e47bc6820bb97c43b3f345201bf5690b15))
* attach batch.yaml/input.yaml as MLflow run artifacts for from-traces ([a20961f](https://github.com/opendatahub-io/agent-eval-harness/commit/a20961fcf1a741bfad8c146f29f81d3d4b635ab8))
* auto-install Python dependencies via SessionStart hook ([403b442](https://github.com/opendatahub-io/agent-eval-harness/commit/403b44208d3e3b7b3b8a9de7012284eac0a0fcd4))
* **collect:** generate events.json from batch-mode stdout ([6a9db37](https://github.com/opendatahub-io/agent-eval-harness/commit/6a9db37e7c6ff3a22aa56cda891e692f412b63e8))
* container image, rfe-assess benchmark, and provider config ([624125f](https://github.com/opendatahub-io/agent-eval-harness/commit/624125f370c6c2726d992821f47fad0ad0ef5e62))
* EvalHub provider for agent skill evaluation ([798756b](https://github.com/opendatahub-io/agent-eval-harness/commit/798756b309fc70fe64825ca8f1b3781f2e12b12d))
* use structured permission_denials from CLI result event ([8b939d8](https://github.com/opendatahub-io/agent-eval-harness/commit/8b939d83575487d92719005dfc38da618413918a))

# Changelog
