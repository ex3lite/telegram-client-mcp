<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { api } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import type {
  AgentSettings,
  AnswerStyle,
  ClaudeCheck,
  ClaudeEffort,
  ClaudeIntegration,
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
  telegram_group_mode: TelegramGroupMode;
  telegram_private_mode: TelegramPrivateMode;
  telegram_attach_markdown: boolean;
}

const route = useRoute();
const queryClient = useQueryClient();
const projectId = computed(() => String(route.query.project ?? ""));
const credential = ref("");
const credentialSaved = ref(false);
const settingsSaved = ref(false);
const confirmDisconnect = ref(false);
const baseline = ref("");

const form = reactive<AgentForm>({
  enabled: true,
  claude_model: null,
  claude_effort: "medium",
  claude_timeout_seconds: 180,
  max_budget_cents: null,
  base_prompt: "",
  answer_style: "normal",
  privacy_level: "strict",
  denied_globs: [],
  telegram_group_mode: "mentions",
  telegram_private_mode: "all_messages",
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
    telegram_group_mode: value.telegram_group_mode,
    telegram_private_mode: value.telegram_private_mode,
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

const saveCredential = useMutation({
  mutationFn: () =>
    api<ClaudeIntegration>("/integrations/claude", {
      method: "PUT",
      body: JSON.stringify({ oauth_token: credential.value })
    }),
  onSuccess: (value) => {
    queryClient.setQueryData(["integrations", "claude"], value);
    credential.value = "";
    credentialSaved.value = true;
  }
});

const disconnectClaude = useMutation({
  mutationFn: () => api<ClaudeIntegration>("/integrations/claude", { method: "DELETE" }),
  onSuccess: (value) => {
    queryClient.setQueryData(["integrations", "claude"], value);
    confirmDisconnect.value = false;
  }
});

const checkClaude = useMutation({
  mutationFn: () => api<ClaudeCheck>("/integrations/claude/check", { method: "POST" })
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
      <StatusBadge :value="claude.data.value?.configured ? 'connected' : 'not_configured'" />
    </div>

    <PageState :loading="claude.isPending.value" :error="claude.error.value" @retry="claude.refetch()">
      <div class="credential-layout">
        <div class="integration-facts">
          <div><span>Источник</span><strong>{{ claude.data.value?.source ?? "missing" }}</strong></div>
          <div><span>Прокси</span><strong>{{ claude.data.value?.proxy_configured ? "Настроен" : "Не настроен" }}</strong></div>
          <div><span>Проверка</span><strong>{{ checkClaude.data.value?.ok ? `Claude ${checkClaude.data.value.version ?? "доступен"}` : "Не запускалась" }}</strong></div>
        </div>
        <form class="credential-form" @submit.prevent="saveCredential.mutate()">
          <label class="field">
            <span>{{ claude.data.value?.configured ? "Заменить OAuth-токен" : "OAuth-токен" }}</span>
            <input
              v-model.trim="credential"
              type="password"
              autocomplete="new-password"
              placeholder="Вставьте новый токен"
              required
            />
            <small>Поле очищается сразу после сохранения.</small>
          </label>
          <button class="button button--primary" type="submit" :disabled="saveCredential.isPending.value || !credential">
            {{ saveCredential.isPending.value ? "Сохраняю…" : claude.data.value?.configured ? "Заменить токен" : "Подключить Claude" }}
          </button>
        </form>
      </div>
      <p v-if="credentialSaved" class="inline-success" role="status">Новый токен принят сервером.</p>
      <p v-if="saveCredential.error.value" class="inline-error" role="alert">{{ saveCredential.error.value.message }}</p>
      <div class="integration-check">
        <button class="button button--secondary button--small" type="button" :disabled="checkClaude.isPending.value" @click="checkClaude.mutate()">
          {{ checkClaude.isPending.value ? "Проверяю…" : "Проверить подключение" }}
        </button>
        <span v-if="checkClaude.data.value" :class="checkClaude.data.value.ok ? 'check-result check-result--ok' : 'check-result check-result--error'">
          {{ checkClaude.data.value.ok ? `Подключение работает · ${checkClaude.data.value.version ?? "версия неизвестна"}` : "Claude недоступен" }}
        </span>
        <span v-if="checkClaude.error.value" class="check-result check-result--error">{{ checkClaude.error.value.message }}</span>
      </div>
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
            <input v-model="claudeModelInput" list="claude-models" placeholder="default" />
            <datalist id="claude-models">
              <option value="sonnet"></option>
              <option value="opus"></option>
              <option value="fable"></option>
            </datalist>
            <small>Пустое поле использует модель Claude CLI по умолчанию.</small>
          </label>
          <label class="field">
            <span>Усилие</span>
            <select v-model="form.claude_effort">
              <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="xhigh">XHigh</option><option value="max">Max</option>
            </select>
          </label>
          <label class="field">
            <span>Таймаут, сек.</span>
            <input v-model.number="form.claude_timeout_seconds" type="number" min="10" max="900" required />
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
        <label class="field">
          <span>Дополнительно запрещённые пути</span>
          <textarea v-model="deniedGlobsText" class="code-input" rows="6" spellcheck="false" placeholder="config/private/**&#10;docs/internal-credentials.md"></textarea>
          <small>Один glob на строку. Системный deny-list для `.env`, ключей и credentials применяется независимо от этого списка.</small>
        </label>
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
              <option value="commands_only">Только команды</option><option value="mentions">Упоминания и команды</option><option value="all_messages">Все сообщения</option>
            </select>
          </label>
          <label class="field">
            <span>В личных сообщениях</span>
            <select v-model="form.telegram_private_mode">
              <option value="commands_only">Только команды</option><option value="all_messages">Все сообщения</option>
            </select>
          </label>
          <label class="switch-control switch-control--standalone">
            <input v-model="form.telegram_attach_markdown" type="checkbox" />
            <span><strong>Прикладывать Markdown</strong><small>Отправлять созданные `.md`-артефакты вместе с ответом.</small></span>
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
