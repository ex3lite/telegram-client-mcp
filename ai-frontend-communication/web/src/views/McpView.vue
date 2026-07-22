<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { reactive, ref, watch } from "vue";

import { api } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { formatDate } from "../format";
import type { McpAccount, McpTokenResult, Project } from "../types";

interface AccountDraft {
  tool_scopes: string[];
  project_ids: string[];
  expires_at: string;
}

const scopes = [
  { value: "identity.resolve_user", label: "Находить сотрудника", detail: "Имя, username, email или внутренний ID" },
  { value: "telegram.ask_user", label: "Задавать вопросы", detail: "Отправлять уточнения сотрудникам в Telegram" },
  { value: "telegram.get_clarification", label: "Читать ответы", detail: "Проверять состояние и получать ответ" },
  { value: "telegram.cancel_clarification", label: "Отменять вопросы", detail: "Закрывать больше не актуальные запросы" },
  { value: "telegram.send_message", label: "Отправлять сообщения", detail: "Писать в разрешённые чаты и прикладывать Markdown" },
  { value: "memory.read", label: "Читать память", detail: "Получать очищенный контекст диалогов выбранного проекта" }
] as const;

const queryClient = useQueryClient();
const showCreate = ref(false);
const oneTimeToken = ref("");
const tokenOwner = ref("");
const copied = ref(false);
const copyError = ref("");
const drafts = reactive<Record<string, AccountDraft>>({});
const createForm = reactive<AccountDraft & { name: string }>({
  name: "",
  tool_scopes: [
    "identity.resolve_user",
    "telegram.ask_user",
    "telegram.get_clarification"
  ],
  project_ids: [],
  expires_at: ""
});

const projects = useQuery({
  queryKey: ["projects"],
  queryFn: () => api<Project[]>("/projects"),
  staleTime: 300_000
});

const accounts = useQuery({
  queryKey: ["mcp", "accounts"],
  queryFn: () => api<McpAccount[]>("/mcp/accounts")
});

watch(
  () => accounts.data.value,
  (rows) => {
    for (const account of rows ?? []) {
      drafts[account.id] = {
        tool_scopes: [...account.tool_scopes],
        project_ids: [...account.project_ids],
        expires_at: toLocalDateTime(account.expires_at)
      };
    }
  },
  { immediate: true }
);

function toLocalDateTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function toIso(value: string): string | null {
  return value ? new Date(value).toISOString() : null;
}

function draftFor(account: McpAccount): AccountDraft {
  return drafts[account.id] ?? {
    tool_scopes: [...account.tool_scopes],
    project_ids: [...account.project_ids],
    expires_at: toLocalDateTime(account.expires_at)
  };
}

function revealToken(result: McpTokenResult) {
  oneTimeToken.value = result.token;
  tokenOwner.value = result.account.name;
  copied.value = false;
  showCreate.value = false;
  void queryClient.invalidateQueries({ queryKey: ["mcp", "accounts"] });
}

const createAccount = useMutation({
  mutationFn: () =>
    api<McpTokenResult>("/mcp/accounts", {
      method: "POST",
      body: JSON.stringify({
        name: createForm.name,
        tool_scopes: createForm.tool_scopes,
        project_ids: createForm.project_ids,
        expires_at: toIso(createForm.expires_at)
      })
    }),
  onSuccess: (result) => {
    revealToken(result);
    Object.assign(createForm, {
      name: "",
      tool_scopes: ["identity.resolve_user", "telegram.ask_user", "telegram.get_clarification"],
      project_ids: [],
      expires_at: ""
    });
  }
});

const patchAccount = useMutation({
  mutationFn: ({ account, patch }: { account: McpAccount; patch: Record<string, unknown> }) =>
    api<McpAccount>(`/mcp/accounts/${account.id}`, {
      method: "PATCH",
      body: JSON.stringify({ ...patch, expected_version: account.version })
    }),
  onSuccess: () => queryClient.invalidateQueries({ queryKey: ["mcp", "accounts"] })
});

const rotateToken = useMutation({
  mutationFn: (account: McpAccount) =>
    api<McpTokenResult>(`/mcp/accounts/${account.id}/rotate-token`, {
      method: "POST",
      body: JSON.stringify({ expected_version: account.version })
    }),
  onSuccess: revealToken
});

function saveAccess(account: McpAccount) {
  const draft = drafts[account.id];
  if (!draft) return;
  patchAccount.mutate({
    account,
    patch: {
      tool_scopes: draft.tool_scopes,
      project_ids: draft.project_ids,
      expires_at: toIso(draft.expires_at)
    }
  });
}

async function copyToken() {
  copied.value = false;
  copyError.value = "";
  try {
    await navigator.clipboard.writeText(oneTimeToken.value);
    copied.value = true;
  } catch {
    copyError.value = "Не удалось скопировать автоматически. Выделите токен вручную.";
  }
}
</script>

<template>
  <header class="page-header">
    <div>
      <span class="eyebrow">Доступ внешних AI-агентов</span>
      <h1>MCP</h1>
      <p>Сервисные аккаунты с точными правами и доступом только к выбранным проектам.</p>
    </div>
    <button class="button button--primary" type="button" @click="showCreate = !showCreate">
      {{ showCreate ? "Закрыть форму" : "Новый аккаунт" }}
    </button>
  </header>

  <section v-if="oneTimeToken" class="token-reveal" aria-live="polite">
    <div class="token-reveal__header">
      <div><span class="eyebrow">Показан один раз</span><h2>Токен для {{ tokenOwner }}</h2></div>
      <button class="text-button" type="button" @click="oneTimeToken = ''">Скрыть</button>
    </div>
    <code tabindex="0">{{ oneTimeToken }}</code>
    <div class="token-reveal__actions">
      <button class="button button--primary button--small" type="button" @click="copyToken">
        {{ copied ? "Скопировано" : "Скопировать токен" }}
      </button>
      <span>После закрытия восстановить токен нельзя — только выпустить новый.</span>
    </div>
    <p v-if="copyError" class="inline-error" role="alert">{{ copyError }}</p>
  </section>

  <form v-if="showCreate" class="settings-card create-account" @submit.prevent="createAccount.mutate()">
    <div class="settings-card__header">
      <div><span class="eyebrow">Новый доступ</span><h2>Сервисный аккаунт</h2><p>Выдайте только те инструменты и проекты, которые нужны агенту.</p></div>
    </div>
    <div class="form-grid form-grid--two">
      <label class="field"><span>Название</span><input v-model.trim="createForm.name" maxlength="120" placeholder="Claude backend assistant" required /></label>
      <label class="field"><span>Истекает</span><input v-model="createForm.expires_at" type="datetime-local" /><small>Оставьте пустым для бессрочного доступа.</small></label>
    </div>
    <fieldset class="form-section">
      <legend>Инструменты</legend>
      <div class="permission-grid">
        <label v-for="scope in scopes" :key="scope.value" class="permission-item">
          <input v-model="createForm.tool_scopes" type="checkbox" :value="scope.value" />
          <span><strong>{{ scope.label }}</strong><small>{{ scope.detail }}</small></span>
        </label>
      </div>
    </fieldset>
    <fieldset class="form-section">
      <legend>Проекты</legend>
      <div class="check-list">
        <label v-for="project in projects.data.value" :key="project.id"><input v-model="createForm.project_ids" type="checkbox" :value="project.id" /> <span>{{ project.name }}</span></label>
      </div>
    </fieldset>
    <p v-if="createAccount.error.value" class="inline-error" role="alert">{{ createAccount.error.value.message }}</p>
    <button class="button button--primary" type="submit" :disabled="createAccount.isPending.value || !createForm.name || !createForm.tool_scopes.length || !createForm.project_ids.length">
      {{ createAccount.isPending.value ? "Создаю…" : "Создать и показать токен" }}
    </button>
  </form>

  <PageState :loading="accounts.isPending.value" :error="accounts.error.value" :empty="accounts.data.value?.length === 0" empty-title="Сервисных аккаунтов пока нет" @retry="accounts.refetch()">
    <section class="account-list" aria-label="Сервисные аккаунты">
      <article v-for="account in accounts.data.value" :key="account.id" class="account-card">
        <header class="account-card__header">
          <div>
            <span class="eyebrow">dca_{{ account.token_prefix }}_…</span>
            <h2>{{ account.name }}</h2>
            <p>Создан {{ formatDate(account.created_at) }} · использован {{ formatDate(account.last_used_at) }}</p>
          </div>
          <StatusBadge :value="account.active ? 'active' : 'disabled'" />
        </header>

        <template v-if="drafts[account.id]">
          <fieldset class="form-section">
            <legend>Разрешённые инструменты</legend>
            <div class="permission-grid permission-grid--compact">
              <label v-for="scope in scopes" :key="scope.value" class="permission-item">
                <input v-model="draftFor(account).tool_scopes" type="checkbox" :value="scope.value" />
                <span><strong>{{ scope.label }}</strong><small>{{ scope.value }}</small></span>
              </label>
            </div>
          </fieldset>
          <fieldset class="form-section">
            <legend>Доступ к проектам</legend>
            <div class="check-list">
              <label v-for="project in projects.data.value" :key="project.id"><input v-model="draftFor(account).project_ids" type="checkbox" :value="project.id" /> <span>{{ project.name }}</span></label>
            </div>
          </fieldset>
          <label class="field field--expiry"><span>Истекает</span><input v-model="draftFor(account).expires_at" type="datetime-local" /></label>
        </template>

        <footer class="account-card__actions">
          <button class="button button--secondary button--small" type="button" :disabled="patchAccount.isPending.value" @click="saveAccess(account)">Сохранить права</button>
          <button class="button button--ghost button--small" type="button" :disabled="rotateToken.isPending.value" @click="rotateToken.mutate(account)">Перевыпустить токен</button>
          <button
            class="button button--ghost button--small"
            :class="{ 'button--danger': account.active }"
            type="button"
            :disabled="patchAccount.isPending.value"
            @click="patchAccount.mutate({ account, patch: { active: !account.active } })"
          >
            {{ account.active ? "Деактивировать" : "Активировать" }}
          </button>
        </footer>
      </article>
    </section>
  </PageState>

  <p v-if="patchAccount.error.value || rotateToken.error.value" class="inline-error" role="alert">
    {{ patchAccount.error.value?.message ?? rotateToken.error.value?.message }}
  </p>
</template>
