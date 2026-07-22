<script setup lang="ts">
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { formatDate, projectName } from "../format";
import type { Project, Repository } from "../types";

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const projectId = computed(() => String(route.query.project ?? ""));
const selectedId = computed(() => String(route.query.selected ?? ""));
const scopeText = ref("");
const scopeSaved = ref(false);

const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
const repositories = useQuery({
  queryKey: ["repositories"],
  queryFn: () => api<Repository[]>("/repositories"),
  placeholderData: keepPreviousData
});
const visibleRepositories = computed(() => {
  const rows = repositories.data.value ?? [];
  return projectId.value ? rows.filter((row) => row.project_id === projectId.value) : rows;
});
const selected = computed(() =>
  visibleRepositories.value.find((row) => row.id === selectedId.value)
);

watch(
  () => selected.value,
  (value) => {
    scopeText.value = value?.allowed_paths.join("\n") ?? "";
    scopeSaved.value = false;
  },
  { immediate: true }
);

const sync = useMutation({
  mutationFn: (id: string) => api(`/repositories/${id}/sync`, { method: "POST" }),
  onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["repositories"] })
});

const saveScope = useMutation({
  mutationFn: (id: string) =>
    api<Repository>(`/repositories/${id}/scope`, {
      method: "PUT",
      body: JSON.stringify({
        allowed_paths: scopeText.value
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean)
      })
    }),
  onSuccess: (updated) => {
    queryClient.setQueryData<Repository[]>(["repositories"], (rows = []) =>
      rows.map((row) => (row.id === updated.id ? updated : row))
    );
    scopeText.value = updated.allowed_paths.join("\n");
    scopeSaved.value = true;
  }
});

async function selectRow(id: string) {
  await router.replace({ query: { ...route.query, selected: id } });
}

async function closeSelection() {
  const query = { ...route.query };
  delete query.selected;
  await router.replace({ query });
}
</script>

<template>
  <header class="page-header">
    <div>
      <h1>Репозитории</h1>
      <p>Read-only источники и зафиксированные commit snapshots.</p>
    </div>
  </header>
  <p v-if="sync.error.value" class="inline-error" role="alert">
    Синхронизация не поставлена в очередь: {{ sync.error.value.message }}
  </p>
  <PageState
    :loading="repositories.isPending.value"
    :error="repositories.error.value"
    :empty="visibleRepositories.length === 0"
    empty-title="Репозитории ещё не добавлены"
    @retry="repositories.refetch()"
  >
    <div class="split-view" :class="{ 'split-view--open': selected }">
      <div class="table-wrap">
        <table class="data-table data-table--selectable">
          <thead>
            <tr><th>Репозиторий</th><th>Проект</th><th>Ветка</th><th>Commit</th><th>Статус</th><th>Авто</th><th>Синхронизация</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="row in visibleRepositories"
              :key="row.id"
              :class="{ 'is-selected': row.id === selectedId }"
              tabindex="0"
              @click="selectRow(row.id)"
              @keydown.enter="selectRow(row.id)"
            >
              <td data-label="Репозиторий"><strong>{{ row.name }}</strong><small>{{ row.ssh_url }}</small></td>
              <td data-label="Проект">{{ projectName(projects.data.value, row.project_id) }}</td>
              <td data-label="Ветка"><code>{{ row.default_branch }}</code></td>
              <td data-label="Commit"><code>{{ row.current_commit?.slice(0, 12) ?? "Нет" }}</code></td>
              <td data-label="Статус"><StatusBadge :value="row.status" /></td>
              <td data-label="Авто">{{ row.auto_sync_mode === "webhook_reconcile" ? "Webhook + проверка" : row.auto_sync_mode === "reconcile" ? "Проверка" : "Выкл" }}</td>
              <td data-label="Синхронизация">{{ formatDate(row.last_synced_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <aside v-if="selected" class="detail-panel" aria-label="Детали репозитория">
        <button class="detail-panel__close" @click="closeSelection">Закрыть</button>
        <h2>{{ selected.name }}</h2>
        <StatusBadge :value="selected.status" />
        <dl class="detail-list">
          <dt>SSH URL</dt><dd><code>{{ selected.ssh_url }}</code></dd>
          <dt>GitHub</dt><dd><code>{{ selected.github_repository ?? "Не привязан" }}</code></dd>
          <dt>Автообновление</dt><dd>{{ selected.auto_sync_mode === "webhook_reconcile" ? "Webhook + резервная проверка" : selected.auto_sync_mode === "reconcile" ? "Резервная проверка" : "Выключено" }}</dd>
          <dt>Webhook URL</dt><dd><code>{{ selected.github_webhook_url }}</code></dd>
          <dt>Последний webhook / commit</dt><dd>{{ formatDate(selected.last_webhook_at) }}<br><code>{{ selected.last_webhook_commit?.slice(0, 12) ?? "Нет" }}</code></dd>
          <dt>Ветка</dt><dd>{{ selected.default_branch }}</dd>
          <dt>Commit</dt><dd><code>{{ selected.current_commit ?? "Не зафиксирован" }}</code></dd>
          <dt>Последняя ошибка</dt><dd>{{ selected.last_error || "Нет" }}</dd>
        </dl>

        <form class="repository-scope" @submit.prevent="saveScope.mutate(selected.id)">
          <label class="field">
            <span>Разрешённые пути Claude</span>
            <textarea v-model="scopeText" rows="7" spellcheck="false" placeholder="src/api&#10;docs/integration" @input="scopeSaved = false"></textarea>
            <small>Один относительный путь на строку. Пусто — весь очищенный snapshot. `.env`, ключи и credentials всё равно исключаются.</small>
          </label>
          <p v-if="saveScope.error.value" class="inline-error" role="alert">Не удалось сохранить scope: {{ saveScope.error.value.message }}</p>
          <p v-if="scopeSaved" class="inline-success" role="status">Scope сохранён и действует со следующего ответа.</p>
          <button class="button" type="submit" :disabled="saveScope.isPending.value">
            {{ saveScope.isPending.value ? "Сохраняю…" : "Сохранить scope" }}
          </button>
        </form>

        <button
          class="button button--primary"
          :disabled="sync.isPending.value || selected.status === 'syncing' || selected.status === 'disabled'"
          @click="sync.mutate(selected.id)"
        >
          {{ sync.isPending.value ? "Постановка..." : "Синхронизировать" }}
        </button>
      </aside>
    </div>
  </PageState>
</template>

<style scoped>
.repository-scope {
  display: grid;
  gap: 0.75rem;
  margin: 1.5rem 0;
}
</style>
