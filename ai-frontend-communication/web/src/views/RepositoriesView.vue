<script setup lang="ts">
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed } from "vue";
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
const sync = useMutation({
  mutationFn: (id: string) => api(`/repositories/${id}/sync`, { method: "POST" }),
  onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["repositories"] })
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
  <header class="page-header"><div><h1>Репозитории</h1><p>Read-only источники и зафиксированные commit snapshots.</p></div></header>
  <p v-if="sync.error.value" class="inline-error" role="alert">Синхронизация не поставлена в очередь: {{ sync.error.value.message }}</p>
  <PageState :loading="repositories.isPending.value" :error="repositories.error.value" :empty="visibleRepositories.length === 0" empty-title="Репозитории ещё не добавлены" @retry="repositories.refetch()">
    <div class="split-view" :class="{ 'split-view--open': selected }">
      <div class="table-wrap"><table class="data-table data-table--selectable"><thead><tr><th>Репозиторий</th><th>Проект</th><th>Ветка</th><th>Commit</th><th>Статус</th><th>Авто</th><th>Синхронизация</th></tr></thead><tbody><tr v-for="row in visibleRepositories" :key="row.id" :class="{ 'is-selected': row.id === selectedId }" tabindex="0" @click="selectRow(row.id)" @keydown.enter="selectRow(row.id)"><td data-label="Репозиторий"><strong>{{ row.name }}</strong><small>{{ row.ssh_url }}</small></td><td data-label="Проект">{{ projectName(projects.data.value, row.project_id) }}</td><td data-label="Ветка"><code>{{ row.default_branch }}</code></td><td data-label="Commit"><code>{{ row.current_commit?.slice(0, 12) ?? "Нет" }}</code></td><td data-label="Статус"><StatusBadge :value="row.status" /></td><td data-label="Авто">{{ row.auto_sync_mode === "webhook_reconcile" ? "Webhook + проверка" : row.auto_sync_mode === "reconcile" ? "Проверка" : "Выкл" }}</td><td data-label="Синхронизация">{{ formatDate(row.last_synced_at) }}</td></tr></tbody></table></div>
      <aside v-if="selected" class="detail-panel" aria-label="Детали репозитория"><button class="detail-panel__close" @click="closeSelection">Закрыть</button><h2>{{ selected.name }}</h2><StatusBadge :value="selected.status" /><dl class="detail-list"><dt>SSH URL</dt><dd><code>{{ selected.ssh_url }}</code></dd><dt>GitHub</dt><dd><code>{{ selected.github_repository ?? "Не привязан" }}</code></dd><dt>Автообновление</dt><dd>{{ selected.auto_sync_mode === "webhook_reconcile" ? "Webhook + резервная проверка" : selected.auto_sync_mode === "reconcile" ? "Резервная проверка" : "Выключено" }}</dd><dt>Webhook URL</dt><dd><code>{{ selected.github_webhook_url }}</code></dd><dt>Последний webhook / commit</dt><dd>{{ formatDate(selected.last_webhook_at) }}<br><code>{{ selected.last_webhook_commit?.slice(0, 12) ?? "Нет" }}</code></dd><dt>Ветка</dt><dd>{{ selected.default_branch }}</dd><dt>Commit</dt><dd><code>{{ selected.current_commit ?? "Не зафиксирован" }}</code></dd><dt>Разрешённые пути</dt><dd>{{ selected.allowed_paths.join(", ") || "Весь snapshot" }}</dd><dt>Последняя ошибка</dt><dd>{{ selected.last_error || "Нет" }}</dd></dl><button class="button button--primary" :disabled="sync.isPending.value || selected.status === 'syncing' || selected.status === 'disabled'" @click="sync.mutate(selected.id)">{{ sync.isPending.value ? "Постановка..." : "Синхронизировать" }}</button></aside>
    </div>
  </PageState>
</template>
