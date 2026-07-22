<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { api, ApiError } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { formatDate } from "../format";
import type {
  AgentSettings,
  AnswerStyle,
  ClaudeCheck,
  ClaudeEffort,
  ClaudeIntegration,
  ClaudeOAuthStart,
  PrivacyLevel,
  Project,
  TelegramGroupMode,
  TelegramPrivateMode
} from "../types";

interface AgentForm {
  enabled: boolean;
  claude_model: string | null;
  claude_effort: ClaudeEffort;
  claude_timeout_seconds: number;
  max_budget_cents: number | null;
  base_prompt: string;
  answer_style: AnswerStyle;
  privacy_level: PrivacyLevel;
  denied_globs: string[];
  memory_enabled: boolean;
  memory_recent_messages: number;
  memory_max_context_chars: number;
  telegram_group_mode: TelegramGroupMode;
  telegram_private_mode: TelegramPrivateMode;
  telegram_streaming_enabled: boolean;
  telegram_attach_markdown: boolean;
}

const route = useRoute();
const queryClient = useQueryClient();
const projectId = computed(() => String(route.query.project ?? ""));
const credential = ref("");
const credentialSaved = ref(false);
const oauthFlowOpen = ref(false);
const oauthSession = ref<ClaudeOAuthStart | null>(null);
const oauthCode = ref("");
const oauthCodeError = ref("");
const oauthExpired = ref(false);
const oauthConnected = ref(false);
const oauthStep = ref<1 | 2 | 3>(1);
const oauthDialog = ref<HTMLDialogElement | null>(null);
const oauthCodeInput = ref<HTMLTextAreaElement | null>(null);
const oauthTrigger = ref<HTMLButtonElement | null>(null);
const settingsSaved = ref(false);
const confirmDisconnect = ref(false);
const baseline = ref("");
const initialClaudeCheckStarted = ref(false);
let oauthExpiryTimer: number | undefined;
let oauthViewMounted = true;

const form = reactive<AgentForm>({
  enabled: true,
  claude_model: null,
  claude_effort: "medium",
  claude_timeout_seconds: 1200,
  max_budget_cents: null,
  base_prompt: "",
  answer_style: "normal",
  privacy_level: "strict",
  denied_globs: [],
  memory_enabled: true,
  memory_recent_messages: 200,
  memory_max_context_chars: 500_000,
  telegram_group_mode: "mentions",
  telegram_private_mode: "all_messages",
  telegram_streaming_enabled: true,
  telegram_attach_markdown: true
});

const projects = useQuery({
  queryKey: ["projects"],
  queryFn: () => api<Project[]>("/projects"),
  staleTime: 300_000
});

const selectedProject = computed(() =>
  projects.data.value?.find((project) => project.id === projectId.value)
);

const settings = useQuery({
  queryKey: computed(() => ["agent-settings", projectId.value]),
  queryFn: () => api<AgentSettings>(`/projects/${projectId.value}/agent-settings`),
  enabled: computed(() => Boolean(projectId.value))
});

const claude = useQuery({
  queryKey: ["integrations", "claude"],
  queryFn: () => api<ClaudeIntegration>("/integrations/claude")
});

const deniedGlobsText = computed({
  get: () => form.denied_globs.join("\n"),
  set: (value: string) => {
    form.denied_globs = value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }
});

const claudeModelInput = computed({
  get: () => form.claude_model ?? "",
  set: (value: string) => {
    form.claude_model = value.trim() || null;
  }
});

const maxBudgetInput = computed({
  get: () => form.max_budget_cents ?? "",
  set: (value: string | number) => {
    form.max_budget_cents = value === "" ? null : Number(value);
  }
});

const serializedForm = computed(() => JSON.stringify(form));
const dirty = computed(() => Boolean(baseline.value) && serializedForm.value !== baseline.value);
const claudeConnectionState = computed(() => {
  if (!claude.data.value?.configured) return "not_configured";
  if (checkClaude.isPending.value) return "syncing";
  if (!checkClaude.data.value) return "stored";
  return checkClaude.data.value.ok ? "connected" : "failed";
});
const claudeConnectionTitle = computed(() => {
  if (!claude.data.value?.configured) return "Требуется авторизация";
  if (checkClaude.isPending.value) return "Проверяем Claude";
  if (checkClaude.data.value?.ok) return "Claude подключён";
  if (checkClaude.data.value) return "Credential не прошёл проверку";
  return "Credential сохранён, но ещё не проверен";
});
const claudeConnectionDescription = computed(() => {
  if (!claude.data.value?.configured) {
    return "Панель проведёт через вход Anthropic без показа итогового токена.";
  }
  if (checkClaude.isPending.value) return "Проверяем credential через Claude Code CLI.";
  if (checkClaude.data.value?.ok) return "Можно безопасно переподключить аккаунт через OAuth.";
  if (checkClaude.data.value) return claudeCheckMessage(checkClaude.data.value.error_code);
  return "Запустите проверку: наличие credential в хранилище ещё не означает успешный вход.";
});

function clearOauthTimer() {
  if (oauthExpiryTimer !== undefined) window.clearTimeout(oauthExpiryTimer);
  oauthExpiryTimer = undefined;
}

function scheduleOauthExpiry(expiresAt: string) {
  clearOauthTimer();
  const delay = new Date(expiresAt).getTime() - Date.now();
  if (!Number.isFinite(delay) || delay <= 0) {
    oauthExpired.value = true;
    return;
  }
  oauthExpiryTimer = window.setTimeout(() => {
    oauthExpired.value = true;
  }, Math.min(delay, 2_147_000_000));
}

function clearOauthState() {
  clearOauthTimer();
  oauthFlowOpen.value = false;
  oauthSession.value = null;
  oauthCode.value = "";
  oauthCodeError.value = "";
  oauthExpired.value = false;
  oauthConnected.value = false;
  oauthStep.value = 1;
  if (oauthViewMounted) void nextTick(() => oauthTrigger.value?.focus());
}

function abandonOauthSession(sessionId: string) {
  void api<void>(`/integrations/claude/oauth/${sessionId}`, { method: "DELETE" }).catch(() => {
    // Best effort: the server expires abandoned OAuth sessions independently.
  });
}

function oauthErrorMessage(error: Error | null, action: "start" | "complete" | "token"): string {
  if (!(error instanceof ApiError)) return error?.message ?? "Не удалось продолжить авторизацию";
  const detail = typeof error.detail === "string" ? error.detail : "";
  if (detail === "claude_oauth_session_active") {
    return "Claude ещё готовит предыдущий запрос на вход. Закройте окно, подождите несколько секунд и повторите.";
  }
  if (detail === "claude_oauth_invalid_state") {
    return action === "complete"
      ? "Эта OAuth-сессия уже закрыта или была перезапущена. Начните подключение заново."
      : "Сессию входа не удалось восстановить. Начните подключение заново.";
  }
  if (error.status === 410) return "OAuth-сессия истекла. Начните подключение заново.";
  if (detail === "claude_oauth_proxy_required") return "Для авторизации сначала нужно настроить исходящий прокси.";
  if (detail === "claude_oauth_invalid_code") return "Anthropic не принял этот одноразовый код. Создайте новую сессию и получите новый код.";
  if (detail === "claude_oauth_invalid_token") {
    return action === "token"
      ? "Это поле принимает только готовый setup-token вида sk-ant-oat…. Одноразовый code#state вставляется в мастере подключения."
      : "Claude не выдал итоговый credential. Начните подключение заново.";
  }
  if (detail === "claude_oauth_provider_error") {
    return action === "start"
      ? "Claude Code не смог подготовить страницу входа. Повторите попытку."
      : "Claude Code не смог завершить вход с этим кодом. Начните подключение заново.";
  }
  if (error.status === 409) return "Состояние OAuth изменилось. Начните подключение заново.";
  if (error.status === 422) return action === "start" ? "Не удалось начать OAuth-вход." : "Не удалось завершить OAuth-вход.";
  return error.message;
}

function claudeCheckMessage(errorCode: string | null): string {
  if (errorCode === "model_provider_authentication_failed") {
    return "Anthropic отклонил credential. Создайте новый setup-token или подключитесь через OAuth.";
  }
  if (errorCode === "claude_oauth_invalid_token") {
    return "Сохранённое значение не является setup-token Claude. Переподключите аккаунт.";
  }
  if (errorCode === "model_provider_not_configured") return "Credential Claude не настроен.";
  if (errorCode === "model_provider_timeout") return "Claude не ответил вовремя. Повторите проверку.";
  return "Claude CLI не смог подтвердить подключение.";
}

async function openOauthDialog() {
  await nextTick();
  const dialog = oauthDialog.value;
  if (!dialog) return;
  if (!dialog.open) dialog.showModal();
  dialog.focus();
}

async function showCodeStep() {
  oauthStep.value = 2;
  oauthCodeError.value = "";
  await nextTick();
  oauthCodeInput.value?.focus();
}

function normalizeOauthCode(value: string): string | null {
  const text = value.trim();
  const codeAndState = text.match(/([A-Za-z0-9_-]{16,}#[A-Za-z0-9_-]{16,})/);
  return codeAndState?.[1] ?? null;
}

onBeforeUnmount(() => {
  oauthViewMounted = false;
  oauthFlowOpen.value = false;
  clearOauthTimer();
  const sessionId = oauthSession.value?.session_id;
  if (sessionId) abandonOauthSession(sessionId);
});

function hydrate(value: AgentSettings) {
  if (value.project_id !== projectId.value) return;
  Object.assign(form, {
    enabled: value.enabled,
    claude_model: value.claude_model,
    claude_effort: value.claude_effort,
    claude_timeout_seconds: value.claude_timeout_seconds,
    max_budget_cents: value.max_budget_cents,
    base_prompt: value.base_prompt,
    answer_style: value.answer_style,
    privacy_level: value.privacy_level,
    denied_globs: [...value.denied_globs],
    memory_enabled: value.memory_enabled,
    memory_recent_messages: value.memory_recent_messages,
    memory_max_context_chars: value.memory_max_context_chars,
    telegram_group_mode: value.telegram_group_mode,
    telegram_private_mode: value.telegram_private_mode,
    telegram_streaming_enabled: value.telegram_streaming_enabled,
    telegram_attach_markdown: value.telegram_attach_markdown
  });
  baseline.value = JSON.stringify(form);
}

watch(
  () => settings.data.value,
  (value) => {
    if (value) hydrate(value);
  },
  { immediate: true }
);

watch(projectId, () => {
  baseline.value = "";
  settingsSaved.value = false;
});

const saveSettings = useMutation({
  mutationFn: () =>
    api<AgentSettings>(`/projects/${projectId.value}/agent-settings`, {
      method: "PUT",
      body: JSON.stringify({ ...form, expected_version: settings.data.value?.version ?? 0 })
    }),
  onSuccess: (value) => {
    queryClient.setQueryData(["agent-settings", projectId.value], value);
    hydrate(value);
    settingsSaved.value = true;
  }
});

const checkClaude = useMutation({
  mutationFn: () => api<ClaudeCheck>("/integrations/claude/check", { method: "POST" })
});

watch(
  () => claude.data.value?.configured,
  (configured) => {
    if (!configured || initialClaudeCheckStarted.value) return;
    initialClaudeCheckStarted.value = true;
    checkClaude.mutate();
  },
  { immediate: true }
);

async function verifyClaude() {
  await claude.refetch();
  try {
    await checkClaude.mutateAsync();
  } catch {
    // The check result is rendered inline; saving the credential still succeeded.
  }
}

const saveCredential = useMutation({
  mutationFn: () =>
    api<ClaudeIntegration>("/integrations/claude", {
      method: "PUT",
      body: JSON.stringify({ oauth_token: credential.value })
    }),
  onSuccess: async (value) => {
    queryClient.setQueryData(["integrations", "claude"], value);
    checkClaude.reset();
    credential.value = "";
    credentialSaved.value = true;
    await verifyClaude();
  },
  onMutate: () => {
    credentialSaved.value = false;
  }
});

const startOauth = useMutation({
  mutationFn: () =>
    api<ClaudeOAuthStart>("/integrations/claude/oauth/start", {
      method: "POST",
      body: JSON.stringify({})
    }),
  onSuccess: (value) => {
    if (!oauthViewMounted || !oauthFlowOpen.value) {
      abandonOauthSession(value.session_id);
      return;
    }
    oauthSession.value = value;
    oauthStep.value = 1;
    oauthExpired.value = false;
    oauthCode.value = "";
    oauthCodeError.value = "";
    scheduleOauthExpiry(value.expires_at);
  }
});

async function beginOauth() {
  const activeSessionId = oauthSession.value?.session_id;
  if (activeSessionId) {
    try {
      await cancelOauth.mutateAsync(activeSessionId);
    } catch {
      // Starting a fresh session gives the user the authoritative server state.
    }
  }
  clearOauthState();
  oauthFlowOpen.value = true;
  oauthConnected.value = false;
  oauthStep.value = 1;
  oauthSession.value = null;
  oauthCode.value = "";
  oauthCodeError.value = "";
  oauthExpired.value = false;
  startOauth.reset();
  completeOauth.reset();
  startOauth.mutate();
  void openOauthDialog();
}

const completeOauth = useMutation({
  mutationFn: () => {
    if (!oauthSession.value || oauthExpired.value) {
      throw new Error("OAuth-сессия истекла. Начните подключение заново.");
    }
    return api<ClaudeIntegration>("/integrations/claude/oauth/complete", {
      method: "POST",
      body: JSON.stringify({ session_id: oauthSession.value.session_id, code: oauthCode.value.trim() })
    });
  },
  onSuccess: async (value) => {
    queryClient.setQueryData(["integrations", "claude"], value);
    clearOauthTimer();
    checkClaude.reset();
    oauthConnected.value = true;
    oauthStep.value = 3;
    oauthSession.value = null;
    oauthCode.value = "";
    await verifyClaude();
  },
  onError: () => {
    oauthStep.value = 2;
  }
});

function submitOauthCode() {
  const normalized = normalizeOauthCode(oauthCode.value);
  if (!normalized) {
    oauthCodeError.value = "Вставьте всю строку из синего блока Anthropic.";
    void nextTick(() => oauthCodeInput.value?.focus());
    return;
  }
  oauthCode.value = normalized;
  oauthCodeError.value = "";
  oauthStep.value = 3;
  completeOauth.mutate();
}

const cancelOauth = useMutation({
  mutationFn: (sessionId: string) =>
    api<void>(`/integrations/claude/oauth/${sessionId}`, { method: "DELETE" }),
  onSettled: clearOauthState
});

function closeOauth() {
  if (cancelOauth.isPending.value) return;
  if (startOauth.isPending.value || completeOauth.isPending.value) {
    clearOauthState();
    return;
  }
  const sessionId = oauthSession.value?.session_id;
  if (sessionId) cancelOauth.mutate(sessionId);
  else clearOauthState();
}

const disconnectClaude = useMutation({
  mutationFn: () => api<ClaudeIntegration>("/integrations/claude", { method: "DELETE" }),
  onSuccess: (value) => {
    queryClient.setQueryData(["integrations", "claude"], value);
    checkClaude.reset();
    credentialSaved.value = false;
    confirmDisconnect.value = false;
  }
});
</script>

<template>
  <header class="page-header page-header--agent">
    <div>
      <span class="eyebrow">Поведение автономного агента</span>
      <h1>Агент</h1>
      <p>Claude, базовый промпт, бюджет, приватность и правила ответов в Telegram.</p>
    </div>
    <StatusBadge :value="form.enabled && projectId ? 'active' : 'disabled'" />
  </header>

  <section class="settings-card settings-card--credential" aria-labelledby="claude-connection-title">
    <div class="settings-card__header">
      <div>
        <span class="eyebrow">Провайдер</span>
        <h2 id="claude-connection-title">Claude Code CLI</h2>
        <p>OAuth-токен хранится на сервере и никогда не возвращается в браузер.</p>
      </div>
      <StatusBadge :value="claudeConnectionState" />
    </div>

    <PageState :loading="claude.isPending.value" :error="claude.error.value" @retry="claude.refetch()">
      <div class="credential-layout">
        <div class="integration-facts">
          <div><span>Источник</span><strong>{{ claude.data.value?.source ?? "missing" }}</strong></div>
          <div><span>Прокси</span><strong>{{ claude.data.value?.proxy_configured ? "Настроен" : "Не настроен" }}</strong></div>
          <div><span>Проверка</span><strong>{{ checkClaude.data.value ? (checkClaude.data.value.ok ? `Claude ${checkClaude.data.value.version ?? "доступен"}` : claudeCheckMessage(checkClaude.data.value.error_code)) : "Не запускалась" }}</strong></div>
        </div>
        <div class="connection-primary">
          <div>
            <strong>{{ claudeConnectionTitle }}</strong>
            <p>{{ claudeConnectionDescription }}</p>
          </div>
          <button ref="oauthTrigger" class="button button--primary" type="button" :disabled="startOauth.isPending.value || cancelOauth.isPending.value" @click="beginOauth">
            {{ startOauth.isPending.value ? "Создаю сессию…" : claude.data.value?.configured ? "Переподключить Claude" : "Подключить Claude" }}
          </button>
        </div>
      </div>

      <Teleport to="body">
        <dialog
          v-if="oauthFlowOpen"
          ref="oauthDialog"
          class="oauth-flow oauth-flow--modal"
          aria-labelledby="oauth-flow-title"
          aria-describedby="oauth-flow-description"
          @cancel.prevent="closeOauth"
        >
          <header class="oauth-flow__header">
            <div>
              <span class="eyebrow">Подключение аккаунта</span>
              <h3 id="oauth-flow-title">Claude через Anthropic</h3>
              <p id="oauth-flow-description">Три шага в одном окне. Итоговый credential останется только на сервере.</p>
            </div>
            <button
              class="oauth-modal__close"
              type="button"
              :disabled="cancelOauth.isPending.value"
              aria-label="Закрыть подключение Claude"
              @click="closeOauth"
            >
              <span aria-hidden="true">×</span>
            </button>
          </header>

          <ol class="oauth-steps" aria-label="Шаги подключения Claude">
            <li :class="{ 'oauth-step--active': oauthStep === 1, 'oauth-step--done': oauthStep > 1 }"><span>1</span><div><strong>Anthropic</strong><small>Разрешить вход</small></div></li>
            <li :class="{ 'oauth-step--active': oauthStep === 2, 'oauth-step--done': oauthStep > 2 }"><span>2</span><div><strong>Код</strong><small>Вставить всю строку</small></div></li>
            <li :class="{ 'oauth-step--active': oauthStep === 3, 'oauth-step--done': checkClaude.data.value?.ok }"><span>3</span><div><strong>Проверка</strong><small>Запустить Claude CLI</small></div></li>
          </ol>

          <div v-if="startOauth.isPending.value" class="oauth-flow__state oauth-flow__state--progress" aria-live="polite">
            <span class="oauth-progress" aria-hidden="true"></span>
            <div><strong>Готовим вход в Anthropic</strong><span>Обычно это занимает несколько секунд.</span></div>
          </div>

          <div v-else-if="startOauth.error.value" class="oauth-flow__state oauth-flow__state--error" role="alert">
            <strong>Не удалось подготовить вход</strong>
            <span>{{ oauthErrorMessage(startOauth.error.value, "start") }}</span>
            <button class="button button--secondary button--small" type="button" @click="beginOauth">Повторить</button>
          </div>

          <div v-else-if="oauthExpired" class="oauth-flow__state oauth-flow__state--error" role="alert">
            <strong>Время сессии закончилось</strong>
            <span>Создайте новую сессию, чтобы Anthropic выдал действующий код.</span>
            <button class="button button--secondary button--small" type="button" @click="beginOauth">Начать заново</button>
          </div>

          <section v-else-if="oauthStep === 1 && oauthSession" class="oauth-stage" aria-labelledby="oauth-stage-one-title">
            <span class="oauth-stage__number" aria-hidden="true">01</span>
            <div class="oauth-stage__content">
              <h4 id="oauth-stage-one-title">Разрешите доступ в Anthropic</h4>
              <p>Откроется новая вкладка. Вы уже авторизованы — подтвердите доступ и скопируйте выданный код.</p>
              <div class="oauth-stage__actions">
                <a class="button button--primary" :href="oauthSession.authorization_url" target="_blank" rel="noopener noreferrer" @click="showCodeStep">Открыть Anthropic</a>
                <button class="text-button" type="button" @click="showCodeStep">Код уже получен</button>
              </div>
              <small class="oauth-expiry">Сессия действует до {{ formatDate(oauthSession.expires_at) }}.</small>
            </div>
          </section>

          <section v-else-if="oauthStep === 2 && oauthSession" class="oauth-stage" aria-labelledby="oauth-stage-two-title">
            <span class="oauth-stage__number" aria-hidden="true">02</span>
            <div class="oauth-stage__content">
              <h4 id="oauth-stage-two-title">Вставьте ответ Anthropic</h4>
              <p>Скопируйте всю строку из синего блока на странице Anthropic.</p>

              <div v-if="completeOauth.error.value" class="oauth-flow__state oauth-flow__state--error" role="alert">
                <strong>Этот код не сработал</strong>
                <span>{{ oauthErrorMessage(completeOauth.error.value, "complete") }}</span>
                <button class="button button--secondary button--small" type="button" @click="beginOauth">Получить новый код</button>
              </div>

              <form v-else class="oauth-code-form oauth-code-form--stacked" @submit.prevent="submitOauthCode">
                <label class="field">
                  <span>Одноразовый код</span>
                  <textarea
                    ref="oauthCodeInput"
                    v-model="oauthCode"
                    autocomplete="one-time-code"
                    autocapitalize="none"
                    maxlength="4096"
                    rows="3"
                    spellcheck="false"
                    placeholder="Вставьте код целиком"
                    :aria-invalid="Boolean(oauthCodeError)"
                    :aria-describedby="oauthCodeError ? 'oauth-code-help oauth-code-error' : 'oauth-code-help'"
                    required
                    @input="oauthCodeError = ''"
                  ></textarea>
                  <small id="oauth-code-help">Панель передаст код в Claude Code без обрезки. Повторно он не используется.</small>
                </label>
                <p v-if="oauthCodeError" id="oauth-code-error" class="inline-error" role="alert">{{ oauthCodeError }}</p>
                <div class="oauth-stage__actions oauth-stage__actions--submit">
                  <button class="button button--primary" type="submit" :disabled="!oauthCode.trim()">Подключить Claude</button>
                  <button class="text-button" type="button" @click="oauthStep = 1">Назад</button>
                </div>
              </form>
              <small class="oauth-expiry">Сессия действует до {{ formatDate(oauthSession.expires_at) }}.</small>
            </div>
          </section>

          <section v-else-if="oauthStep === 3" class="oauth-stage" aria-labelledby="oauth-stage-three-title">
            <span class="oauth-stage__number" aria-hidden="true">03</span>
            <div class="oauth-stage__content">
              <h4 id="oauth-stage-three-title">Проверяем подключение</h4>

              <div v-if="completeOauth.isPending.value || checkClaude.isPending.value || (oauthConnected && !checkClaude.data.value && !checkClaude.error.value)" class="oauth-flow__state oauth-flow__state--progress" aria-live="polite">
                <span class="oauth-progress" aria-hidden="true"></span>
                <div>
                  <strong>{{ completeOauth.isPending.value ? "Завершаем OAuth-вход" : "Запускаем Claude Code CLI" }}</strong>
                  <span>{{ completeOauth.isPending.value ? "Передаём одноразовый код Claude." : "Проверяем credential реальным запросом." }}</span>
                </div>
              </div>

              <div v-else-if="checkClaude.data.value?.ok" class="oauth-flow__state oauth-flow__state--success" role="status">
                <strong>Проверка пройдена</strong>
                <span>Claude {{ checkClaude.data.value.version ?? "CLI" }} подключён и готов отвечать.</span>
                <button class="button button--primary button--small" type="button" @click="clearOauthState">Готово</button>
              </div>

              <div v-else class="oauth-flow__state oauth-flow__state--error" role="alert">
                <strong>OAuth сохранён, но проверка не пройдена</strong>
                <span>{{ checkClaude.data.value ? claudeCheckMessage(checkClaude.data.value.error_code) : "Не удалось запустить проверку Claude CLI." }}</span>
                <div class="oauth-stage__actions">
                  <button class="button button--secondary button--small" type="button" :disabled="checkClaude.isPending.value" @click="verifyClaude">Проверить снова</button>
                  <button class="text-button" type="button" @click="beginOauth">Подключить заново</button>
                </div>
              </div>
            </div>
          </section>
        </dialog>
      </Teleport>

      <details class="manual-token">
        <summary>Расширенный режим</summary>
        <p class="oauth-expiry">Только для готового setup-token вида <code>sk-ant-oat…</code>. Для одноразового кода используйте кнопку «Подключить Claude» выше.</p>
        <form class="credential-form" @submit.prevent="saveCredential.mutate()">
          <label class="field" aria-describedby="setup-token-help">
            <span>Готовый setup-token</span>
            <input v-model.trim="credential" type="password" autocomplete="new-password" placeholder="sk-ant-oat…" required />
            <small id="setup-token-help">Поле очистится сразу после сохранения.</small>
          </label>
          <button class="button button--secondary" type="submit" :disabled="saveCredential.isPending.value || !credential">
            {{ saveCredential.isPending.value ? "Сохраняю и проверяю…" : "Сохранить setup-token" }}
          </button>
        </form>
        <p v-if="credentialSaved && checkClaude.isPending.value" class="inline-success" role="status">Setup-token сохранён. Проверяем Claude CLI…</p>
        <p v-else-if="credentialSaved && checkClaude.data.value?.ok" class="inline-success" role="status">Проверка пройдена · Claude {{ checkClaude.data.value.version ?? "CLI" }}</p>
        <p v-else-if="credentialSaved && checkClaude.data.value" class="inline-error" role="alert">{{ claudeCheckMessage(checkClaude.data.value.error_code) }}</p>
        <p v-if="saveCredential.error.value" class="inline-error" role="alert">{{ oauthErrorMessage(saveCredential.error.value, "token") }}</p>
      </details>
      <div v-if="claude.data.value?.source === 'panel'" class="danger-row">
        <template v-if="confirmDisconnect">
          <span>Удалить токен, сохранённый через панель?</span>
          <button class="button button--danger button--small" type="button" @click="disconnectClaude.mutate()">Удалить</button>
          <button class="button button--ghost button--small" type="button" @click="confirmDisconnect = false">Отмена</button>
        </template>
        <button v-else class="text-button text-button--danger" type="button" @click="confirmDisconnect = true">Отключить credential</button>
      </div>
    </PageState>
  </section>

  <section v-if="!projectId" class="selection-required">
    <span class="selection-required__index">01</span>
    <div>
      <h2>Выберите проект</h2>
      <p>Настройки агента изолированы по проектам. Используйте переключатель в верхней панели.</p>
    </div>
  </section>

  <PageState
    v-else
    :loading="settings.isPending.value"
    :error="settings.error.value"
    @retry="settings.refetch()"
  >
    <form class="settings-stack" @submit.prevent="saveSettings.mutate()">
      <section class="settings-card" aria-labelledby="runtime-title">
        <div class="settings-card__header">
          <div>
            <span class="eyebrow">{{ selectedProject?.name ?? "Проект" }}</span>
            <h2 id="runtime-title">Runtime Claude</h2>
            <p>Модель, глубина рассуждения и жёсткие пределы одного запуска.</p>
          </div>
          <label class="switch-control">
            <input v-model="form.enabled" type="checkbox" />
            <span>Агент включён</span>
          </label>
        </div>
        <div class="form-grid form-grid--four">
          <label class="field field--wide">
            <span>Модель</span>
            <select v-model="claudeModelInput">
              <option value="claude-opus-4-6">Opus 4.6 · видимый Mind</option>
              <option value="opus">Opus latest · Mind может быть скрыт</option>
              <option value="sonnet">Sonnet latest · Mind может быть скрыт</option>
              <option value="fable">Fable latest · Mind может быть скрыт</option>
              <option value="">По умолчанию Claude CLI</option>
            </select>
            <small>Для нативного Thinking в Telegram используйте Opus 4.6: этот профиль проверен на реальном stream-json Claude CLI.</small>
          </label>
          <label class="field">
            <span>Усилие</span>
            <select v-model="form.claude_effort">
              <option value="low">Низкое</option><option value="medium">Среднее</option><option value="high">Высокое</option><option value="xhigh">Очень высокое</option><option value="max">Максимальное</option>
            </select>
          </label>
          <label class="field">
            <span>Таймаут, сек.</span>
            <input v-model.number="form.claude_timeout_seconds" type="number" min="10" max="3600" required />
          </label>
          <label class="field">
            <span>Бюджет, центы</span>
            <input v-model="maxBudgetInput" type="number" min="1" step="1" placeholder="Без лимита" />
          </label>
        </div>
      </section>

      <section class="settings-card" aria-labelledby="prompt-title">
        <div class="settings-card__header">
          <div>
            <span class="eyebrow">Инструкции</span>
            <h2 id="prompt-title">Базовый промпт</h2>
            <p>Добавляется сервером к каждому вопросу. Репозиторий не может переопределить эту инструкцию.</p>
          </div>
          <label class="field field--compact">
            <span>Стиль ответа</span>
            <select v-model="form.answer_style">
              <option value="brief">Кратко</option><option value="normal">Сбалансированно</option><option value="detailed">Подробно</option>
            </select>
          </label>
        </div>
        <label class="field">
          <span class="sr-only">Текст базового промпта</span>
          <textarea v-model="form.base_prompt" rows="10" maxlength="20000" placeholder="Например: отвечай как ведущий инженер проекта, объясняй решения через факты из репозитория…"></textarea>
          <small>{{ form.base_prompt.length.toLocaleString("ru-RU") }} / 20 000 символов</small>
        </label>
      </section>

      <section class="settings-card" aria-labelledby="privacy-title">
        <div class="settings-card__header">
          <div>
            <span class="eyebrow">Контроль выдачи</span>
            <h2 id="privacy-title">Приватность</h2>
            <p>Политика исполняется до запуска Claude и повторно перед публикацией ответа.</p>
          </div>
          <span class="hard-guard">Секреты блокируются всегда</span>
        </div>
        <div class="choice-grid">
          <label class="choice-card" :class="{ 'choice-card--selected': form.privacy_level === 'strict' }">
            <input v-model="form.privacy_level" type="radio" value="strict" />
            <strong>Строгий</strong>
            <span>Останавливает ответ при любом privacy finding. Ничего не публикует.</span>
          </label>
          <label class="choice-card" :class="{ 'choice-card--selected': form.privacy_level === 'balanced' }">
            <input v-model="form.privacy_level" type="radio" value="balanced" />
            <strong>Сбалансированный</strong>
            <span>Редактирует найденные секреты и публикует очищенную версию.</span>
          </label>
        </div>
        <p class="muted-note"><strong>Быдло Guard всегда включён.</strong> Высокоуверенные попытки вытащить ключи, токены, пароли или `.env` фиксируются в аудите. Claude отвечает отдельной неизменяемой ролью без доступа к репозиторию; исключений по ролям нет.</p>
        <label class="field">
          <span>Дополнительно запрещённые пути</span>
          <textarea v-model="deniedGlobsText" class="code-input" rows="6" spellcheck="false" placeholder="config/private/**&#10;docs/internal-credentials.md"></textarea>
          <small>Один glob на строку. Системный deny-list для `.env`, ключей и credentials применяется независимо от этого списка.</small>
        </label>
      </section>

      <section class="settings-card" aria-labelledby="memory-settings-title">
        <div class="settings-card__header">
          <div>
            <span class="eyebrow">Контекст диалога</span>
            <h2 id="memory-settings-title">Память</h2>
            <p>Claude ведёт отдельную native-сессию для каждого пользователя и безопасно восстанавливает её из БД после ротации.</p>
          </div>
          <label class="switch-control">
            <input v-model="form.memory_enabled" type="checkbox" />
            <span>Память включена</span>
          </label>
        </div>
        <div class="form-grid form-grid--two">
          <label class="field">
            <span>Недавних сообщений</span>
            <input v-model.number="form.memory_recent_messages" type="number" min="4" max="500" required :disabled="!form.memory_enabled" />
            <small>Сколько реплик загрузить из БД при создании или восстановлении native-сессии.</small>
          </label>
          <label class="field">
            <span>Лимит контекста, символов</span>
            <input v-model.number="form.memory_max_context_chars" type="number" min="3000" max="1000000" step="10000" required :disabled="!form.memory_enabled" />
            <small>Предел bootstrap-контекста из БД; внутри активной сессии Claude использует native compaction.</small>
          </label>
        </div>
        <p class="muted-note">Отключение памяти не удаляет историю. Удаление диалога на экране «Память» очищает и БД, и native transcript Claude.</p>
      </section>

      <section class="settings-card" aria-labelledby="telegram-title">
        <div class="settings-card__header">
          <div>
            <span class="eyebrow">Канал доставки</span>
            <h2 id="telegram-title">Telegram</h2>
            <p>Определяет, когда автономный агент отвечает в группе и личных сообщениях.</p>
          </div>
        </div>
        <div class="form-grid form-grid--three">
          <label class="field">
            <span>В группе</span>
            <select v-model="form.telegram_group_mode">
              <option value="commands_only">Только команды</option><option value="mentions">Упоминания и команды</option><option value="all_messages">Все сообщения — отвечает без вызова</option>
            </select>
            <small>Рекомендуется «Упоминания»: Братулец не будет влезать в обычный разговор.</small>
          </label>
          <label class="field">
            <span>В личных сообщениях</span>
            <select v-model="form.telegram_private_mode">
              <option value="commands_only">Только команды</option><option value="all_messages">Все сообщения</option>
            </select>
          </label>
          <label class="switch-control switch-control--standalone">
            <input v-model="form.telegram_streaming_enabled" type="checkbox" />
            <span><strong>Нативный AI-stream</strong><small>Показывать Thinking и вживую обновлять форматированный ответ через Bot API.</small></span>
          </label>
          <label class="switch-control switch-control--standalone">
            <input v-model="form.telegram_attach_markdown" type="checkbox" />
            <span><strong>Markdown по запросу</strong><small>Прикладывать `.md`-артефакты, только когда пользователь явно просит создать документацию или файл.</small></span>
          </label>
        </div>
      </section>

      <div class="save-bar" :class="{ 'save-bar--dirty': dirty }">
        <div>
          <strong>{{ dirty ? "Есть несохранённые изменения" : settingsSaved ? "Настройки сохранены" : "Настройки синхронизированы" }}</strong>
          <span>Версия {{ settings.data.value?.version ?? 0 }}</span>
        </div>
        <p v-if="saveSettings.error.value" class="save-bar__error" role="alert">{{ saveSettings.error.value.message }}</p>
        <button class="button button--primary" type="submit" :disabled="!dirty || saveSettings.isPending.value">
          {{ saveSettings.isPending.value ? "Сохраняю…" : "Сохранить настройки" }}
        </button>
      </div>
    </form>
  </PageState>
</template>

<style scoped>
.oauth-flow--modal {
  width: min(44rem, calc(100vw - 2rem));
  max-width: none;
  max-height: calc(100dvh - 2rem);
  margin: auto;
  overflow-y: auto;
  color: #24251f;
  box-shadow: 0 1.5rem 5rem rgb(78 48 35 / 24%);
}

.oauth-flow--modal::backdrop {
  background: rgb(31 29 26 / 62%);
  backdrop-filter: blur(0.25rem);
}

.oauth-modal__close {
  display: grid;
  width: 2.25rem;
  height: 2.25rem;
  flex: 0 0 auto;
  padding: 0;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 50%;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
}

.oauth-modal__close:hover:not(:disabled) {
  border-color: var(--border-strong);
  color: #24251f;
  background: #f5ebe4;
}

.oauth-modal__close:active:not(:disabled) {
  transform: scale(0.96);
}

.oauth-modal__close:disabled {
  cursor: wait;
  opacity: 0.45;
}

.oauth-modal__close span {
  font-size: 1.5rem;
  line-height: 1;
  transform: translateY(-0.06rem);
}

.oauth-stage {
  display: grid;
  grid-template-columns: 3.25rem minmax(0, 1fr);
  gap: 1rem;
  min-height: 14rem;
  padding: 1.25rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: #faf7f1;
}

.oauth-stage__number {
  color: #c8a99c;
  font-family: "SFMono-Regular", monospace;
  font-size: 1.45rem;
  font-variant-numeric: tabular-nums;
}

.oauth-stage__content {
  min-width: 0;
}

.oauth-stage__content h4 {
  margin: 0;
  font-size: 1.12rem;
  font-weight: 600;
  letter-spacing: -0.02em;
}

.oauth-stage__content > p {
  max-width: 56ch;
  margin: 0.45rem 0 1.25rem;
  color: var(--muted);
  font-size: 0.84rem;
  text-wrap: pretty;
}

.oauth-stage__actions {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  margin-top: 1rem;
}

.oauth-stage__actions--submit {
  justify-content: space-between;
}

.oauth-stage__content > .oauth-expiry {
  display: block;
  margin-top: 1rem;
}

.oauth-code-form--stacked {
  display: grid;
  grid-template-columns: 1fr;
  align-items: stretch;
}

.oauth-code-form--stacked textarea {
  min-height: 6rem;
  resize: vertical;
  word-break: break-all;
}

.oauth-flow__state--progress {
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  justify-items: stretch;
  min-height: 8rem;
  padding: 1.25rem;
}

.oauth-flow__state--progress div,
.oauth-flow__state--progress strong,
.oauth-flow__state--progress span {
  display: block;
}

.oauth-flow__state--progress div > span {
  margin-top: 0.2rem;
}

.oauth-progress {
  width: 1.55rem;
  height: 1.55rem;
  border: 2px solid #e3cfc5;
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: oauth-spin 800ms linear infinite;
}

.manual-token > .oauth-expiry {
  margin: 0.75rem 0 0;
}

@keyframes oauth-spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 42rem) {
  .oauth-flow--modal {
    width: calc(100vw - 1rem);
    max-height: calc(100dvh - 1rem);
    padding: 1rem;
  }

  .oauth-stage {
    grid-template-columns: 1fr;
    gap: 0.5rem;
    padding: 1rem;
  }

  .oauth-stage__actions,
  .oauth-stage__actions--submit {
    align-items: stretch;
    flex-direction: column;
  }

  .oauth-stage__actions .button,
  .oauth-stage__actions .text-button {
    width: 100%;
    text-align: center;
  }
}

@media (prefers-reduced-motion: reduce) {
  .oauth-progress { animation: none; }
}
</style>
