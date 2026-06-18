# [1.14.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.13.2...v1.14.0) (2026-06-18)


### Bug Fixes

* address CodeRabbit review findings on Harbor integration ([c02b05f](https://github.com/opendatahub-io/agent-eval-harness/commit/c02b05f3838faf346f897af1893384e1b2e3b54c))
* address second-round CodeRabbit findings ([7a87c55](https://github.com/opendatahub-io/agent-eval-harness/commit/7a87c550130d3c5b055668eb7cf4d9b483b149ea))
* **containerfile:** Harbor compatibility fixes ([31064d7](https://github.com/opendatahub-io/agent-eval-harness/commit/31064d72c0e0009353e1444d9adf49a65d254fd9))
* **harbor/k8s:** fix tar upload permissions for non-root pods ([eaf4981](https://github.com/opendatahub-io/agent-eval-harness/commit/eaf498122163836341a928aba1942218c3b98d64))
* **harbor/k8s:** match claude.ai/install.sh in skip-pkg-installs regex ([37cc074](https://github.com/opendatahub-io/agent-eval-harness/commit/37cc074c98699287bd05bd2462db83951a3c82a9))
* **harbor:** only copy declared output paths in verifier test.sh ([3ac0b2f](https://github.com/opendatahub-io/agent-eval-harness/commit/3ac0b2f873797a1216c3c1c767e7048e4f027366))
* **harbor:** propagate SIGTERM/SIGINT to harbor run subprocess ([d83e76b](https://github.com/opendatahub-io/agent-eval-harness/commit/d83e76b70619f8f01c8500f8f9d2e64b46f59a8f))
* **harbor:** use wall-clock duration from Harbor job timestamps ([544d54e](https://github.com/opendatahub-io/agent-eval-harness/commit/544d54ef2b795aea9854907569133d3d859022d7))
* **report:** show judge type for multi-step and non-config judges ([965b2e1](https://github.com/opendatahub-io/agent-eval-harness/commit/965b2e15b7eb03b1c067286f83ad78399460ee2e))
* update test for simplified instruction.md and fix zero-cost bug ([5e3694f](https://github.com/opendatahub-io/agent-eval-harness/commit/5e3694fe750569140496298f7ce250ac26fc54d6))


### Features

* add Harbor integration for containerized eval execution ([96c9fdd](https://github.com/opendatahub-io/agent-eval-harness/commit/96c9fddd1de195b8064a96f4349359cb705e99af))
* **eval-dataset:** add --harbor flag for task package generation ([63f2c59](https://github.com/opendatahub-io/agent-eval-harness/commit/63f2c599a6980e4cc3f1c60d8eb3c41e5e76139b))
* **eval-setup:** add --harbor flag for Harbor dependency installation ([abbe3f3](https://github.com/opendatahub-io/agent-eval-harness/commit/abbe3f3d6afb6138da86629c6b99924b351e3ef0))
* **harbor/k8s:** stream agent logs to pod stdout ([029e1a4](https://github.com/opendatahub-io/agent-eval-harness/commit/029e1a471785149e5dd1a02ec107609b28e932b8))
* **harbor:** add --env flag and default skip-pkg-installs for K8s ([f79e0c1](https://github.com/opendatahub-io/agent-eval-harness/commit/f79e0c12b73a5d97a78a86487249e3719a2245c4))
* **harbor:** add multi-step trial parsing ([d0605f5](https://github.com/opendatahub-io/agent-eval-harness/commit/d0605f583ca51f373e636bf41eb5ba75cd7ec6ea))
* **harbor:** extract turns, duration, version from agent transcripts ([5910342](https://github.com/opendatahub-io/agent-eval-harness/commit/591034229f2fd1557e6c156aa34ba12cc28004b4))
* **harbor:** generate per-case batch.yaml for batch-mode evals ([75c38c8](https://github.com/opendatahub-io/agent-eval-harness/commit/75c38c8acdec9b2946f29127755852455992616f))
* **harbor:** merge judge engine results into multi-step trials ([2a25ad9](https://github.com/opendatahub-io/agent-eval-harness/commit/2a25ad9a0a1ddc032969bf72b305a4b679d6ee23))
* **harbor:** simplify instruction.md template ([3920939](https://github.com/opendatahub-io/agent-eval-harness/commit/3920939c10f01ec6e0f4b632d9702d854ae57d52))
* **score:** support ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL for LLM judges ([5b4ce15](https://github.com/opendatahub-io/agent-eval-harness/commit/5b4ce15ab901fa8984cac79c95a9b5f1b8618605))

## [1.13.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.13.1...v1.13.2) (2026-06-09)


### Bug Fixes

* **trace_builder:** return server trace ID from _log_trace to fix FK failures ([db004f0](https://github.com/opendatahub-io/agent-eval-harness/commit/db004f0c763aa5949e8f52dae3d5634fd2a4409b)), closes [#95](https://github.com/opendatahub-io/agent-eval-harness/issues/95)
* warn when _log_trace returns no backend ID ([e877d96](https://github.com/opendatahub-io/agent-eval-harness/commit/e877d9625a3f36e0b119b0b3375a0d7734a239a7))

## [1.13.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.13.0...v1.13.1) (2026-06-09)


### Bug Fixes

* don't sample deterministic judges even with CLI --samples ([cbef112](https://github.com/opendatahub-io/agent-eval-harness/commit/cbef112080e449f0786a90a4f29a1d24341a9f98))
* let --samples 1 override per-judge config ([0346683](https://github.com/opendatahub-io/agent-eval-harness/commit/03466835a8b5cc1aad00ddae0ac762c412a84f03))
* preserve numbered list continuity across blank lines in rationales ([17baf24](https://github.com/opendatahub-io/agent-eval-harness/commit/17baf24374336795769b58a2efa707521fc77951))

# [1.13.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.12.0...v1.13.0) (2026-06-09)


### Bug Fixes

* add extra_env parameter to ResponsesAPIRunner.run_skill ([4e0360e](https://github.com/opendatahub-io/agent-eval-harness/commit/4e0360e25e7ff4b30478dc049e74351fe0912deb))
* address CodeRabbit review feedback ([bd90b5f](https://github.com/opendatahub-io/agent-eval-harness/commit/bd90b5f669812a72bcf28d068e893c5c2e3b22cf))
* address PR [#88](https://github.com/opendatahub-io/agent-eval-harness/issues/88) review feedback from astefanutti ([04e9cec](https://github.com/opendatahub-io/agent-eval-harness/commit/04e9cec89704c702d71b83b229584628c1979e07)), closes [#70](https://github.com/opendatahub-io/agent-eval-harness/issues/70)
* guarantee after_each hooks run and flow hook env to CLI runner ([122bab7](https://github.com/opendatahub-io/agent-eval-harness/commit/122bab70e8c4b171939dc76231f5d50887067309))
* move before_each inside try/finally so after_each runs on setup failure ([b114c02](https://github.com/opendatahub-io/agent-eval-harness/commit/b114c02cf9185f261f1b20c8fb4676229308738e))
* return synthetic failed result instead of None on case errors ([5f8887f](https://github.com/opendatahub-io/agent-eval-harness/commit/5f8887ffdf8c37be31287470af69b6e3c36790d7))


### Features

* add execution lifecycle hooks to eval pipeline ([5bc1e05](https://github.com/opendatahub-io/agent-eval-harness/commit/5bc1e052aa1948d0d6e88ea3449a4942327009ec))
* **hooks:** implement hook outputs for passing state from hooks to runners and judges ([bb21862](https://github.com/opendatahub-io/agent-eval-harness/commit/bb21862a060d2572e18667c99ae1b90b93569089))

# [1.12.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.11.0...v1.12.0) (2026-06-08)


### Features

* tabbed rationale view for non-stable sampled judges ([e23f9c6](https://github.com/opendatahub-io/agent-eval-harness/commit/e23f9c6711ebe908c0d8e987a9ed0b6a6d1fd0eb))

# [1.11.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.10.0...v1.11.0) (2026-06-08)


### Bug Fixes

* address CodeRabbit review findings on sampling stability ([4704c3f](https://github.com/opendatahub-io/agent-eval-harness/commit/4704c3f7cd19898f3d47fc905ab9a5bd4c87999c))
* force structured output for score/bool LLM judges ([e1bc1eb](https://github.com/opendatahub-io/agent-eval-harness/commit/e1bc1eb7b010359dce669cbbcc87c947dc444607))
* force structured output for the pairwise judge ([ba1d521](https://github.com/opendatahub-io/agent-eval-harness/commit/ba1d5214bedf8ce7e1538e496550511496af392c))
* pass 1-5 scale bounds to score histogram in the report ([9cf507f](https://github.com/opendatahub-io/agent-eval-harness/commit/9cf507ff2d8d2c90d9800aaff05b9117d025c942))
* stop truncating score/bool judge rationales to 200 chars ([c922532](https://github.com/opendatahub-io/agent-eval-harness/commit/c9225321bd2c20ac7a105d6de9e7ed678eff02bd))


### Features

* add --repeat to pairwise for verdict-stability measurement ([796b614](https://github.com/opendatahub-io/agent-eval-harness/commit/796b614cbbdde0ca6744583ee4010bf93c7fca52))
* annotate judge scores with sampling stability in the report ([1ae938b](https://github.com/opendatahub-io/agent-eval-harness/commit/1ae938bea8ace06be703419c9f22d5757fd94c75))
* per-judge `samples` config in eval.yaml ([f478aaf](https://github.com/opendatahub-io/agent-eval-harness/commit/f478aafdff5c46030ac47d73ddf0ddbac82f41cb))
* render pairwise stability section in the report ([19f1704](https://github.com/opendatahub-io/agent-eval-harness/commit/19f1704dd27e090a763795a55f4083cd82e5de61))
* sample LLM judges N times for score stability (judges --repeat) ([6b3677d](https://github.com/opendatahub-io/agent-eval-harness/commit/6b3677d6d25d5cf2f1067ba50438f6756435bf63))
* visualise judge sampling stability in the report ([c6b955f](https://github.com/opendatahub-io/agent-eval-harness/commit/c6b955f7fcbe5c19450b27857f36d1684e2e36cd))

# [1.10.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.9.1...v1.10.0) (2026-06-05)


### Bug Fixes

* pairwise comparison sends full artifacts and captures judge reasoning ([288bd3b](https://github.com/opendatahub-io/agent-eval-harness/commit/288bd3be7701a7f09eb0444abb20ab6b9a3ef163))


### Features

* show pairwise verdict and reasoning per case in the report ([f28b689](https://github.com/opendatahub-io/agent-eval-harness/commit/f28b689c1c68a183b94e67a96df4440b0984cc47))

## [1.9.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.9.0...v1.9.1) (2026-06-05)


### Bug Fixes

* skip workspace.files in batch mode to prevent silent overwrites ([#112](https://github.com/opendatahub-io/agent-eval-harness/issues/112)) ([bb7e8bd](https://github.com/opendatahub-io/agent-eval-harness/commit/bb7e8bd48f4638ca536a95dc6e9a68028d4d0e8d)), closes [#111](https://github.com/opendatahub-io/agent-eval-harness/issues/111)

# [1.9.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.8.0...v1.9.0) (2026-06-05)


### Features

* add dataset.workspace.files for provisioning case source files ([#70](https://github.com/opendatahub-io/agent-eval-harness/issues/70)) ([f687a3c](https://github.com/opendatahub-io/agent-eval-harness/commit/f687a3ca7792616ae48ed5f62e88c9f31d1dc752))

# [1.8.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.7.2...v1.8.0) (2026-06-04)


### Features

* change runner.env from list to dict with $VAR resolution ([#108](https://github.com/opendatahub-io/agent-eval-harness/issues/108)) ([b1abc0b](https://github.com/opendatahub-io/agent-eval-harness/commit/b1abc0b83c614ec94406f82b799ea61c57eeec14))

## [1.7.2](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.7.1...v1.7.2) (2026-06-04)


### Bug Fixes

* **eval-run:** include cache tokens in input token metric ([#107](https://github.com/opendatahub-io/agent-eval-harness/issues/107)) ([4ef40fd](https://github.com/opendatahub-io/agent-eval-harness/commit/4ef40fd705e24e9687bb612ec8a3b7d030cc52fd))

## [1.7.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.7.0...v1.7.1) (2026-06-04)


### Bug Fixes

* **eval-run:** validate baseline run-id exists in preflight check ([#106](https://github.com/opendatahub-io/agent-eval-harness/issues/106)) ([2a8ac0d](https://github.com/opendatahub-io/agent-eval-harness/commit/2a8ac0de10a398132b3c6d51e0edf4f6bd002fc6))

# [1.7.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.6.0...v1.7.0) (2026-06-04)


### Features

* replace runner.env_strip with runner.env for additive env forwarding ([#105](https://github.com/opendatahub-io/agent-eval-harness/issues/105)) ([b052fe1](https://github.com/opendatahub-io/agent-eval-harness/commit/b052fe1c9f5ea5bbeb954cf23b6aa66979b512f4)), closes [#103](https://github.com/opendatahub-io/agent-eval-harness/issues/103)

# [1.6.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.5.0...v1.6.0) (2026-06-03)


### Features

* add /eval-check for harness-level context and skills scanning / checking ([#74](https://github.com/opendatahub-io/agent-eval-harness/issues/74)) ([de0ca7c](https://github.com/opendatahub-io/agent-eval-harness/commit/de0ca7cf0e7697047bfd8200e9cc8f00995e44ee))

# [1.5.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.4.1...v1.5.0) (2026-06-02)


### Features

* Flexible Eval Directory Layout [Spec + Impl] ([#85](https://github.com/opendatahub-io/agent-eval-harness/issues/85)) ([c978627](https://github.com/opendatahub-io/agent-eval-harness/commit/c9786277f6e3053a6359c4776f473042b91963f8)), closes [#86](https://github.com/opendatahub-io/agent-eval-harness/issues/86) [#77](https://github.com/opendatahub-io/agent-eval-harness/issues/77) [#70](https://github.com/opendatahub-io/agent-eval-harness/issues/70) [#70](https://github.com/opendatahub-io/agent-eval-harness/issues/70)

## [1.4.1](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.4.0...v1.4.1) (2026-06-02)


### Bug Fixes

* **eval-run:** reflow soft-wrapped paragraphs in HTML report ([#92](https://github.com/opendatahub-io/agent-eval-harness/issues/92)) ([7a6fd41](https://github.com/opendatahub-io/agent-eval-harness/commit/7a6fd41053c0941f1d97c21af557eff01b8041e4))

# [1.4.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.3.0...v1.4.0) (2026-05-29)


### Features

* **eval-dataset:** builtin judges, conditional coverage, run-aware expansion ([#84](https://github.com/opendatahub-io/agent-eval-harness/issues/84)) ([9c3995d](https://github.com/opendatahub-io/agent-eval-harness/commit/9c3995dae7737e73fcd72f47f49a21cffbc67794))

# [1.3.0](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.2.3...v1.3.0) (2026-05-29)


### Features

* **eval-optimize:** add judge type awareness, targeted re-runs, smarter analysis ([#83](https://github.com/opendatahub-io/agent-eval-harness/issues/83)) ([ce89bc5](https://github.com/opendatahub-io/agent-eval-harness/commit/ce89bc531f9be2369a0d91c29beaed60412ac54c))

## [1.2.3](https://github.com/opendatahub-io/agent-eval-harness/compare/v1.2.2...v1.2.3) (2026-05-29)


### Bug Fixes

* **eval-review:** update for v1.2 judge types, exact case matching ([#82](https://github.com/opendatahub-io/agent-eval-harness/issues/82)) ([2f7c0ea](https://github.com/opendatahub-io/agent-eval-harness/commit/2f7c0ea8a47ce83ed3d8baf315a3b376fa4a598c))

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
