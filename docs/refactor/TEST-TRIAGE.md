# 测试三分类（T0.2）

用途：决定重构时哪些测试**必须保持全绿**、哪些**允许改写**、哪些**一条都不许删**。

| 类别 | 含义 | 重构时的处置 |
|---|---|---|
| A 契约 | 守护对外行为与语义 | 必须全绿，失败即回滚 |
| B 实现锁定 | 打桩在模块内部状态上 | 阶段 3 前不动；改代码前先改写测试 |
| C 事故回归 | 守护注释里记录过的真实故障 | 一条都不许删 |

### A 类 · 契约（20 个）

> 对外行为/语义契约，重构的安全网主体

- `tests/test_account_format_contract.py`
- `tests/test_account_status_removal.py`
- `tests/test_auth_flow_cookies.py`
- `tests/test_auth_flow_module_patching.py`
- `tests/test_chatgpt_status_policy_banned.py`
- `tests/test_chatgpt_status_probe.py`
- `tests/test_contrast_gate_contract.py`
- `tests/test_deploy_config_contract.py`
- `tests/test_end_to_end_flows.py`
- `tests/test_frontend_layout_contract.py`
- `tests/test_grok_status_semantics.py`
- `tests/test_icloud_remote_frontend_contract.py`
- `tests/test_login_refresh_flow_contract.py`
- `tests/test_panel_status_actions.py`
- `tests/test_panel_status_sync.py`
- `tests/test_pool_status_contract.py`
- `tests/test_probe_status_task.py`
- `tests/test_rt_route_reaches_protocol_flow.py`
- `tests/test_secret_config_contract.py`
- `tests/test_status_semantics.py`

### B 类 · 实现锁定（13 个）

> 依赖 `api.tasks` 模块级状态；阶段 3 去 api 依赖时需先改写为构造注入

- `tests/test_account_proxy_reuse.py`
- `tests/test_backfill_rt_task_api.py`
- `tests/test_batch_scope_consistency.py`
- `tests/test_bind_2fa_task_api.py`
- `tests/test_data_bundle.py`
- `tests/test_end_to_end_flows.py`
- `tests/test_environment_preflight.py`
- `tests/test_log_cleanup.py`
- `tests/test_refresh_token_task.py`
- `tests/test_register_task_controls.py`
- `tests/test_task_kinds_and_visibility.py`
- `tests/test_task_module_layout.py`
- `tests/test_tasks_list_perf.py`

### C 类 · 事故回归（119 个）

> 对应 field-notes 里的真实故障；删除即丢失规格

- `tests/conftest.py`
- `tests/remote_hme_double.py`
- `tests/route_harness.py`
- `tests/test_account_export_api.py`
- `tests/test_account_field_merge.py`
- `tests/test_account_repository_batch.py`
- `tests/test_account_update_api.py`
- `tests/test_accounts_list_payload.py`
- `tests/test_alias_failure_rotation.py`
- `tests/test_annotation_resolvability.py`
- `tests/test_api_integration.py`
- `tests/test_api_proxies_and_auth.py`
- `tests/test_auth_hardening.py`
- `tests/test_backfill_rt_task_api.py`
- `tests/test_banned_discovery.py`
- `tests/test_banned_signal_propagation.py`
- `tests/test_banned_upload_exclusion.py`
- `tests/test_batch_action_platform_agnostic.py`
- `tests/test_batch_action_task.py`
- `tests/test_bind_2fa_task_api.py`
- `tests/test_chatgpt2api_panel.py`
- `tests/test_chatgpt_account_state.py`
- `tests/test_chatgpt_add_phone_sms_loop.py`
- `tests/test_chatgpt_auto_maintenance.py`
- `tests/test_chatgpt_client_id_present.py`
- `tests/test_chatgpt_phone_register.py`
- `tests/test_chatgpt_platform_rt_route.py`
- `tests/test_chatgpt_registration_engine.py`
- `tests/test_chatgpt_status_policy_banned.py`
- `tests/test_chatgpt_status_probe.py`
- `tests/test_chatgpt_sync.py`
- `tests/test_chatgpt_token_plan_settings.py`
- `tests/test_chatgpt_token_refresh_verification.py`
- `tests/test_chatgpt_two_factor.py`
- `tests/test_chatgpt_two_factor_persistence.py`
- `tests/test_cliproxyapi_sync.py`
- `tests/test_cliproxyapi_sync_config_alias.py`
- `tests/test_config_dedup_and_registration_modes.py`
- `tests/test_console_broken_pipe.py`
- `tests/test_contrast_gate_contract.py`
- `tests/test_credential_fields.py`
- `tests/test_cross_platform_email_reuse.py`
- `tests/test_data_bundle.py`
- `tests/test_db_modular.py`
- `tests/test_deploy_config_contract.py`
- `tests/test_device_id_reuse.py`
- `tests/test_environment_preflight.py`
- `tests/test_exception_handlers_not_too_wide.py`
- `tests/test_executor_drives_path.py`
- `tests/test_existing_account_falls_back_to_otp.py`
- `tests/test_frontend_delivery_perf.py`
- `tests/test_frontend_layout_contract.py`
- `tests/test_frontend_running_tasks_cap.py`
- `tests/test_grok2api_client_reuse.py`
- `tests/test_grok2api_web_only.py`
- `tests/test_grok_platform.py`
- `tests/test_grok_pool_bookkeeping.py`
- `tests/test_grok_probe_fixes.py`
- `tests/test_grok_register_browser.py`
- `tests/test_grok_send_code_and_grok2api.py`
- `tests/test_grok_send_code_rejection.py`
- `tests/test_grok_status_semantics.py`
- `tests/test_grok_token_refresh.py`
- `tests/test_grok_websso_upload_chain.py`
- `tests/test_http_client_and_solver_manager.py`
- `tests/test_icloud_alias_repository_backends.py`
- `tests/test_icloud_api.py`
- `tests/test_icloud_remote_api.py`
- `tests/test_icloud_remote_fixes.py`
- `tests/test_icloud_source_isolation.py`
- `tests/test_icloud_web_client.py`
- `tests/test_import_export_roundtrip.py`
- `tests/test_integrations_dispatch_gaps.py`
- `tests/test_json_deployment_end_to_end.py`
- `tests/test_json_table_store.py`
- `tests/test_list_payload_no_secrets.py`
- `tests/test_mail_import_source_config.py`
- `tests/test_maintenance_doc.py`
- `tests/test_otp_extraction_guard.py`
- `tests/test_outlook_pool_selection.py`
- `tests/test_panel_comparison.py`
- `tests/test_panel_comparison_concurrency.py`
- `tests/test_panel_comparison_grok2api.py`
- `tests/test_panel_management_actions.py`
- `tests/test_panel_platform_filter.py`
- `tests/test_panel_push.py`
- `tests/test_panel_push_persist.py`
- `tests/test_panel_registry.py`
- `tests/test_panel_sync.py`
- `tests/test_panel_sync_credential_error.py`
- `tests/test_panel_time_single_side.py`
- `tests/test_probe_status_task.py`
- `tests/test_proxy_playwright_bridge.py`
- `tests/test_proxy_utils.py`
- `tests/test_query_layer.py`
- `tests/test_refresh_failure_classification.py`
- `tests/test_refresh_token_task.py`
- `tests/test_refresh_uses_account_client_id.py`
- `tests/test_register_settings_ui.py`
- `tests/test_register_task_controls.py`
- `tests/test_register_task_limits.py`
- `tests/test_registration_chain_ordering.py`
- `tests/test_remote_account_alias_availability.py`
- `tests/test_repository_backends.py`
- `tests/test_rt_route_reaches_protocol_flow.py`
- `tests/test_runtime_version_stamp.py`
- `tests/test_secret_config_contract.py`
- `tests/test_sentinel_and_account_helpers.py`
- `tests/test_sms_module_layout.py`
- `tests/test_solver_idle_reaping.py`
- `tests/test_solver_task_retention.py`
- `tests/test_status_semantics.py`
- `tests/test_storage_backend.py`
- `tests/test_storage_coverage_gate.py`
- `tests/test_storage_coverage_report.py`
- `tests/test_task_log_repository_backends.py`
- `tests/test_tasks_list_perf.py`
- `tests/test_timezone_display.py`
- `tests/test_upload_credential_source.py`

## 阶段 3 前置（B 类清单）

以下文件在 T3.2（去 `api` 依赖）之前必须先改写成构造注入，否则会挡住重构：

- [ ] `tests/test_account_proxy_reuse.py`
- [ ] `tests/test_backfill_rt_task_api.py`
- [ ] `tests/test_batch_scope_consistency.py`
- [ ] `tests/test_bind_2fa_task_api.py`
- [ ] `tests/test_data_bundle.py`
- [ ] `tests/test_end_to_end_flows.py`
- [ ] `tests/test_environment_preflight.py`
- [ ] `tests/test_log_cleanup.py`
- [ ] `tests/test_refresh_token_task.py`
- [ ] `tests/test_register_task_controls.py`
- [ ] `tests/test_task_kinds_and_visibility.py`
- [ ] `tests/test_task_module_layout.py`
- [ ] `tests/test_tasks_list_perf.py`
