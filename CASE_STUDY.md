# Production Case Study: Subscription Community & Billing Engine

> **System Overview:** A production-grade membership management, billing, and automated access control platform built for a high-traffic creator community on Telegram. Processed multi-currency recurring subscriptions across Stripe and on-chain USDT crypto with zero double-charges and 100% automated membership lifecycle enforcement.

[![FastAPI](https://img.shields.io/badge/Backend-FastAPI_0.111-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![aiogram](https://img.shields.io/badge/Bot-aiogram_3.7-2CA5E0?logo=telegram)](https://docs.aiogram.dev)
[![Next.js](https://img.shields.io/badge/Admin_%26_Portal-Next.js_14-black?logo=next.js)](https://nextjs.org)
[![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL_16-336791?logo=postgresql)](https://postgresql.org)
[![Tests](https://img.shields.io/badge/Test_Suite-929_Passed-success)](https://pytest.org)

---

## 1. Commercial Results & Operational Impact

In its initial production launch week, the system operated under live traffic and processed payments across three distinct currencies:

| Currency | Gross Volume Processed | Payment Rails | Automated Fulfillment Rate |
| :--- | :--- | :--- | :---: |
| **EUR (€)** | **€1,648** | Stripe Checkout (Credit/Debit Card) | 100% |
| **RUB (₽)** | **₽251,500** | Alternative Payment Gateways | 100% |
| **USD ($)** | **$4,306** | On-Chain USDT (Tron TRC-20 & Ethereum ERC-20) | 100% |

### Key Operational Metrics
- **Zero Double Charges:** Idempotent payment processing with unique webhook event tracking and distributed lock guards.
- **Automated Lifecycle Delivery:** 100% of verified payers received single-use Telegram channel and chat invites automatically within 5 seconds of confirmation.
- **Automated Revocation:** Background cron workers scanned expired subscriptions, issued 3-day advance renewal prompts, and automatically removed delinquent members from private channels.
- **Support Burden Reduction:** Automated magic-link login and self-service subscription management reduced manual operator access requests to zero.

---

## 2. Product Architecture & Engineering Flow

```mermaid
graph TD
    subgraph Client & Bot Entry
        User([Community Member]) -->|Telegram /start| Bot[aiogram 3 Bot Service]
        AdminUser([Community Manager]) --> Admin[Next.js 14 Admin Panel]
        Member([Active Subscriber]) --> Portal[Next.js 14 Video Portal]
    end

    subgraph Core API Backend
        Bot -->|Internal API Calls| API[FastAPI 18-Router Backend]
        Admin -->|REST API / JWT + 2FA| API
        Portal -->|Magic Link Auth| API
    end

    subgraph Data & State
        API --> DB[(PostgreSQL 16\n28 Relational Models)]
        API --> Redis[(Redis 7 Cache &\nDistributed Locks)]
    end

    subgraph Payment Infrastructure
        Stripe[Stripe Webhooks] -->|idempotent fulfill_payment| API
        TronGrid[TronGrid API / TRC-20] -->|3-Block Confirmation| API
        Etherscan[Etherscan API / ERC-20] -->|Finalized Block Check| API
    end

    subgraph Lifecycle Automation
        Worker[APScheduler Background Worker] -->|Check Expired| DB
        Worker -->|Revoke Chat Access| Bot
        Worker -->|Renewal Notification| Bot
    end
```

---

## 3. Core Technical Capabilities

### 1. Unified Idempotent Billing Engine
- Unified `fulfill_payment()` entrypoint handles multiple disparate payment methods (Stripe subscriptions, on-chain crypto, and admin manual overrides).
- Webhook events are checked against an internal idempotency ledger before state transitions occur, guaranteeing that network retries never generate duplicate subscription periods or excess invite tokens.
- On-chain crypto transactions are verified directly against TronGrid and Etherscan nodes, checking receiver address, exact amount, transaction age, and requiring 3 block confirmations before granting membership.

### 2. Autonomous Membership Lifecycle
- Single-use, time-expiring Telegram invite links prevent link sharing across unauthorized users.
- Automated referral attribution (`?start=ref_<code>`) calculates custom discount rules for the invitee and credits the inviter with bonus days or tiered affiliate balances.
- Periodic background evaluation worker (`kick_expired_job`) cleans up members whose billing grace period has lapsed without manual admin intervention.

### 3. Comprehensive Administrative Control
- Real-time executive dashboard calculating MRR, churn rate, active member cohorts, and revenue by currency.
- Granular subscriber management with manual extension, pause, refund, and revocation capabilities.
- Role-based access control with TOTP-based Two-Factor Authentication (2FA).

### 4. Rigorous Test Suite
- Comprehensive suite of **929 automated tests** covering router schemas, auth handlers, crypto verification, referral calculations, bot state machines, and webhook edge cases.
- 100% mocked external dependencies allowing full test suite execution in CI environments in under 30 seconds without requiring external live databases.

---

## 4. Role & AI-Assisted Engineering Workflow

### What I Owned (Product & Architecture):
- **Requirements & System Specification:** Scoped user journeys, payment flows, referral incentives, and admin moderation needs directly from community operator requirements.
- **Architecture & Data Modeling:** Designed the database schema (28 tables), API router structure, authentication flows, and payment idempotency guarantees.
- **Quality Assurance & Verification:** Formulated edge-case test scenarios (network drops, webhook replays, currency mismatch, expired tokens) and audited every test run.
- **Deployment & Production Operations:** Configured Docker Compose environments, Caddy SSL reverse proxy, automated encrypted database backups, and monitored real-time payment events during launch week.

### How AI Coding Agents Were Leveraged:
Rather than writing every line of boilerplate by hand, I acted as an **AI systems architect and orchestrator**:
- Used state-of-the-art AI coding agents (Claude Code, OpenAI Codex, and Antigravity) to accelerate implementation speed.
- Provided strict architectural constraints, interface contracts, and schema boundaries to the agents.
- Continuously validated generated code through automated test harnesses, type checking, and security audits.
- Achieved a production-ready, enterprise-scale full-stack system in a fraction of traditional development time while maintaining 100% operational reliability.

---

## 5. Public vs. Private Edition Disclosure

This repository is the sanitized, open-source edition of the live private production platform (`pavel_community`). 
- **Preserved:** Complete backend architecture, 18 FastAPI async routers, bot state machine, full test suite (929 tests), admin and portal frontends, Docker deployment orchestration, and database migrations.
- **Sanitized:** Proprietary client branding, live API tokens, real customer identifying information, and private production secrets have been replaced with standard templates and environment configurations.
