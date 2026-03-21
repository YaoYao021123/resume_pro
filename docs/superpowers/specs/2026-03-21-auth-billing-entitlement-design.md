# 登录、付费与配额系统设计（平台 Key + BYOK）

## 1. 背景与目标

当前项目是本地文件驱动的简历生成器，缺少真实用户身份、计费和配额治理能力。新目标是在部署版支持：

- 登录体系（MVP：手机号验证码 + 邮箱验证码）
- 平台内置 API Key 的可控使用
  - 免费用户：每月 3 次 AI 生成
  - 会员用户：每周 50 次 AI 生成
- BYOK（用户自带 API Key）：不收费、不限次
- 微信/支付宝支付会员

核心原则：

- 先做可运行的 MVP，后续再扩展微信登录
- “是否可生成”必须由后端统一裁决，前端只展示状态
- 只对“生成成功”扣额度，失败不扣

---

## 2. 范围

### In Scope（MVP）

- 认证：手机号/邮箱验证码登录
- 会员支付：微信/支付宝下单与回调
- 权限判定：免费/月 3 次；会员/周 50 次；BYOK 不限次
- 生成前鉴权 + 生成后扣减
- 审计日志（调用模式、扣额结果、错误码）

### Out of Scope（本期不做）

- 微信登录（OAuth）与联合登录绑定
- 复杂营销系统（优惠券、裂变、邀请返利）
- 多档套餐并存（先单一会员档）
- 大规模风控模型（先规则限流）

---

## 3. 用户与计费规则（确认版）

## 3.1 登录

- 支持手机号验证码、邮箱验证码
- 同一手机号或邮箱对应唯一用户
- 登录后签发会话令牌（短期 Access + 可续期策略由实现阶段定）

## 3.2 调用模式

- `platform_key`：使用平台内置 Key，受套餐/配额约束
- `byok`：用户提供自己的 Key，不触发付费与平台额度扣减

## 3.3 配额规则

- 免费用户：`platform_key` 每自然月 3 次（北京时间，自然月 1 日 00:00 重置）
- 会员用户：`platform_key` 每自然周 50 次（北京时间，周一 00:00 重置）
- BYOK：不限次
- 扣减时机：仅当生成接口最终成功时扣减（幂等扣减）

---

## 4. 目标架构（方案 A）

采用“独立后端服务 + 现有生成服务”双层架构。

## 4.1 组件边界

1. **Auth/Billing Backend（新增）**
- 职责：认证、会员、配额、支付回调、审计
- 存储：MySQL + Redis
- 对外：鉴权/计费/额度 API

2. **Resume Generation Service（现有 `web/server.py`）**
- 职责：简历数据读写、生成编排、LaTeX 编译
- 改造点：在生成前后调用 Auth/Billing Backend 完成“额度预占 + 最终结算”

3. **AI Provider Layer（现有 `tools/generate_resume.py`）**
- 职责：执行实际模型调用与生成逻辑
- 改造点：支持按请求注入用户级模型配置（BYOK），不再只依赖全局 `.env.local`

## 4.2 请求数据流

1. 用户登录 -> 获得会话
2. 前端请求生成（携带 `mode=platform_key|byok`）
3. 生成服务调用后端 `/entitlements/reserve`
4. 允许则执行生成
5. 生成结束后调用 `/entitlements/finalize`（success=消费预占，fail=释放预占，仅平台 Key）
6. 返回结果并记录审计事件

---

## 5. 数据模型（后端）

建议 MySQL 表（最小集合）：

1. `users`
- `id`, `created_at`, `status`

2. `auth_identities`
- `user_id`, `type`(phone/email), `identifier`, `verified_at`
- 唯一索引：(`type`, `identifier`)

3. `user_person_bindings`
- `user_id`, `person_id`, `created_at`
- 唯一索引：(`user_id`, `person_id`)
- 唯一索引：(`person_id`)（MVP 禁止跨用户共享，同一 person 只能归属 1 个用户）
- 用于强制“用户仅访问自己授权的 person 数据目录”

4. `subscriptions`
- `user_id`, `plan`(free/member_weekly50), `status`(active/inactive), `start_at`, `end_at`
- 仅保留当前有效订阅 + 历史快照（可拆历史表）
- 会员生命周期规则（MVP）：
  - 新购：`start_at = 支付确认时间`，`end_at = start_at + 30 天`
  - 续费：若当前会员未过期，则在原 `end_at` 基础上顺延 30 天；否则从支付确认时间起算
  - 退款/撤销：立即置为 `inactive`，后续按免费配额执行

5. `usage_counters`
- `user_id`, `mode`(platform_key), `period_type`(month/week), `period_start`, `limit`, `used`, `reserved`
- 唯一索引：(`user_id`, `mode`, `period_type`, `period_start`)
- 说明：BYOK 不计费不限次，不建配额计数

6. `generation_events`
- `idempotency_key`, `reservation_id`, `user_id`, `mode`, `result`(success/fail), `finalize_applied`, `created_at`, `error_code`
- 用于审计与防重复结算

7. `payment_orders`
- `order_no`, `user_id`, `channel`(wechat/alipay), `amount`, `status`, `provider_trade_no`, `created_at`, `paid_at`

8. `user_api_keys`
- `id`, `user_id`, `provider`, `encrypted_api_key`, `key_fingerprint`, `is_active`, `created_at`, `updated_at`
- MySQL 落地约束（替代部分索引）：
  - 增加生成列 `active_provider = IF(is_active, provider, NULL)`
  - 唯一索引：(`user_id`, `active_provider`)
- 用于 BYOK 安全存储、轮换、禁用

9. `entitlement_reservations`
- `reservation_id`, `user_id`, `request_id`, `mode`, `person_id`, `period_type`, `period_start`, `status`(reserved/finalized/released), `created_at`, `expires_at`
- 唯一索引：(`reservation_id`)
- 唯一索引：(`user_id`, `request_id`)
- 用于跟踪每次预占及最终结算结果

10. `pending_finalize_jobs`
- `id`, `reservation_id`, `idempotency_key`, `user_id`, `mode`, `person_id`, `status`, `retry_count`, `next_retry_at`, `last_error`, `created_at`
- 唯一索引：(`idempotency_key`)
- 用于 `/entitlements/finalize` 超时/失败后的补偿重试

Redis 键：

- 验证码（手机号/邮箱 + TTL）
- 验证码发送频率限制
- 会话态/黑名单（若采用）

---

## 6. API 设计（MVP）

## 6.1 认证

- `POST /auth/send-code`
  - 入参：`{ channel: phone|email, target }`
  - 出参：发送结果

- `POST /auth/login`
  - 入参：`{ channel, target, code }`
  - 出参：`{ access_token, refresh_token, expires_in, user }`

- `POST /auth/refresh`
  - 入参：`{ refresh_token }`
  - 出参：`{ access_token, refresh_token, expires_in }`（refresh 轮换）

- `POST /auth/logout`
  - 入参：`{ refresh_token }`（撤销当前会话）
  - 出参：成功

## 6.2 会员与支付

- `POST /billing/create-order`
  - 入参：`{ plan: member_weekly50, channel: wechat|alipay }`
  - 出参：支付下单参数（二维码/URL/SDK参数）

- `POST /billing/webhook/wechat`
- `POST /billing/webhook/alipay`
  - 校验签名后更新订单与订阅状态
  - 必须幂等

## 6.3 权限预占与最终结算（替代 check->consume）

- `POST /entitlements/reserve`
  - 入参：`{ mode, person_id, request_id }`（`user_id` 从服务间鉴权上下文解析，不允许前端传）
  - 出参：`{ allow, reservation_id, remaining_after_reserve, reset_at }`
  - 语义：原子完成“配额检查 + 预占 1 次”；不允许先 check 再独立扣减
  - 幂等约束：(`user_id`, `request_id`) 唯一；重复请求返回首次结果，不重复预占

- `POST /entitlements/finalize`
  - 入参：`{ reservation_id, result: success|fail, idempotency_key }`
  - 出参：`{ finalized, consumed, released, remaining }`
  - 语义：
    - `success`：确认消费该预占
    - `fail`：释放预占，不计入已用额度

## 6.4 BYOK 契约

- 前端生成请求在 `mode=byok` 时附带：
  - `byok.provider`
  - `byok.model`
  - `byok.api_key`（写入时加密存储；接口返回时永不回显明文）
- 校验规则：
  - 空 key 拒绝（`BYOK_INVALID`）
  - 仅允许受支持 provider
  - key 最短长度和字符集校验
- 存储策略：
  - 服务端加密落库（KMS 或应用层 AES-GCM）
  - 日志仅记录 key 指纹（hash 后缀），禁止明文
- 生命周期：
  - `POST /byok/upsert`：新增或替换当前 provider 的 key
  - `DELETE /byok/{provider}`：禁用当前 key（软删除）
  - 同一 provider 只保留 1 个 active key（旧 key 自动失活）

## 6.5 服务间信任契约

- 生成服务先验证用户会话 token，得到 `user_id`
- 生成服务调用 Auth/Billing Backend 时使用服务凭证，并在服务端签名头传递 `user_id`、`request_id`
- Auth/Billing Backend 仅在“服务凭证有效 + 签名校验通过”时信任该 `user_id`
- 前端请求体中的 `user_id` 一律忽略

---

## 7. 与现有代码的集成点

## 7.1 `web/server.py`

- 在 `/api/generate` 入口（现 `_generate_resume` 路径）前增加：
  1. 校验登录态
  2. 校验 `person_id` 是否在 `user_person_bindings`
  3. 调用 `/entitlements/reserve`
- 在生成成功后增加：
  1. 调用 `/entitlements/finalize(result=success)`（`mode=platform_key` 时）
  2. 写入审计日志
- 在生成失败后增加：
  1. 调用 `/entitlements/finalize(result=fail)` 释放预占

服务间安全：

- 生成服务调用 Auth/Billing Backend 必须携带服务级凭证（mTLS 或签名 token）
- 后端仅信任服务凭证和会话解析结果，不信任请求体中的 `user_id`

## 7.2 `tools/generate_resume.py`

- 当前通过 `get_model_config()` 读取全局配置
- 需新增“请求级 ai_config 覆盖”参数，使 BYOK 能按用户注入
- BYOK 明文 Key 不落日志；日志仅记录 key 指纹（如后 6 位哈希）

## 7.3 数据目录隔离

- 当前 `data/{person_id}` 是组织隔离，不是安全隔离
- 登录后需要“用户 -> 可访问 person_id 集合”映射
- 生成请求必须绑定当前用户授权的 `person_id`

---

## 8. 错误处理与风控

## 8.1 统一错误码（示例）

- `AUTH_REQUIRED`
- `AUTH_CODE_INVALID`
- `QUOTA_EXCEEDED_MONTHLY_FREE`
- `QUOTA_EXCEEDED_WEEKLY_MEMBER`
- `PAYMENT_NOT_COMPLETED`
- `PAYMENT_WEBHOOK_INVALID_SIGNATURE`
- `BYOK_INVALID`

## 8.2 风控最小集

- 验证码发送限流（IP + target 双维度）
- 登录失败次数限制与短时封禁
- 支付回调签名校验、重放防护、幂等更新
- 扣额接口幂等（`idempotency_key` 唯一）
- 生成服务与鉴权服务网络异常处理：
- `/entitlements/reserve` 超时：默认拒绝生成（fail-closed）
- 生成完成但 `/entitlements/finalize` 超时：写入 `pending_finalize` 事件并异步重试，确保最终一致
- 强一致计费：只要生成结果为成功，系统必须最终达成 `finalize(success)`，不得漏扣

## 8.3 并发与幂等（强约束）

- `reserve` 必须在 DB 事务中原子执行：
  1. 计算当前周期 key（周/月）
  2. 若 `usage_counters` 不存在则事务内创建（create-on-first-use）
  3. `SELECT ... FOR UPDATE` 锁定计数行
  4. 校验 `used + reserved < limit`，满足则 `reserved = reserved + 1` 并写 `reservation` 记录
- `finalize` 必须幂等：
  - `success`：`reserved-1, used+1`
  - `fail`：`reserved-1`
  - 同一 `reservation_id`/`idempotency_key` 重放返回首次结果

- 预占过期回收：
  - worker 每分钟扫描 `status=reserved AND expires_at < now()`
  - 回收前检查是否存在 `pending_finalize_jobs` 或“生成成功待结算”事件，若存在则禁止回收该 reservation
  - 事务内执行：对应 `usage_counters.reserved = reserved - 1`，并将 reservation 置为 `released`
  - 回收失败进入重试队列，避免“僵尸预占”占额

## 8.4 会员与配额边界规则

- 会员有效期：支付成功时立即生效，30 天后到期
- 会员期间始终按“自然周 50 次”判定，不做按天折算
- 会员到期后立即切换为免费策略（自然月 3 次）
- 若“会员到期当周”发生切换：到期后新请求按免费月配额判定

## 8.5 异步补扣重试策略

- `pending_finalize_jobs` worker 采用指数退避重试（1m, 5m, 15m, 1h, 6h）
- 最大重试 5 次；超过后转 `dead_letter` 状态并告警
- 结算成功后将 job 标记 `done`，并写审计日志

---

## 9. 测试与验收标准

## 9.1 关键测试

- 免费用户平台 Key：第 1~3 次成功，第 4 次拒绝
- 会员平台 Key：单周第 1~50 次成功，第 51 次拒绝
- BYOK：不限次且不写扣额记录
- 生成失败不扣额；重试成功后仅扣一次
- 支付 webhook 重复投递不重复开通会员

## 9.2 验收标准

- 能完成手机号/邮箱登录闭环
- 能完成微信/支付宝支付后会员生效
- 平台 Key 配额规则与重置周期准确
- BYOK 与平台 Key 路径清晰分离
- 所有扣额行为可审计、可追踪

---

## 10. 演进路线（非本期实现）

- 二期接入微信登录（OAuth）并支持账号合并
- 支持多档套餐（周/月/按次包）
- 增加组织版（团队共享额度）
- 更细粒度风控与异常检测

---

## 11. MVP 固化默认值（已确认）

为避免实现阶段反复决策，以下默认值在本期固定：

## 11.1 会话与登录策略

- Access Token TTL：2 小时
- Refresh Token TTL：30 天
- Refresh 轮换：每次刷新签发新 refresh，旧 refresh 立即失效
- 单用户最多 3 个活跃设备会话，超出后踢掉最早会话

## 11.2 支付订单策略

- 单一会员档价格：`price_cents=2990`（即 29.9 元）作为后端配置项
- 币种：`CNY`
- 订单有效期：30 分钟，超时自动置 `expired`
- 重复支付处理：按 `order_no + provider_trade_no` 幂等，仅首笔生效

## 11.3 用户与 person 绑定迁移

- 首次上线迁移脚本规则：
  - 为每个已有 person 生成一个“默认归属用户”并写入 `user_person_bindings`
  - 后续仅允许绑定本人数据，不开放跨用户共享
- 若迁移失败：阻断发布（fail release），不允许半迁移状态上线

## 11.4 BYOK 优先级与失败 UX

- 优先级：请求体显式 `byok.api_key` > `user_api_keys` 中 active key
- 若两者都不存在或校验失败：返回 `BYOK_INVALID`
- BYOK 校验失败不扣配额，前端展示“请更新你的 API Key”

## 11.5 可观测性与告警

- 核心指标：
  - `entitlement_reserve_latency_p95`
  - `entitlement_finalize_success_rate`
  - `pending_finalize_dead_letter_count`
  - `payment_webhook_invalid_signature_count`
- 告警阈值（5 分钟窗口）：
  - `finalize_success_rate < 99%`
  - `dead_letter_count > 0`
  - `invalid_signature_count > 5`

## 11.6 安全与留存

- KMS 主密钥轮换：每 90 天
- BYOK 明文零落盘（仅内存处理 + 加密存储）
- 审计日志保留 180 天
- 登录/支付相关 PII 留存 365 天，超期归档或删除
