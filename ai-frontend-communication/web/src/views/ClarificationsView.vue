<script setup lang="ts">
import { keepPreviousData, useQuery } from "@tanstack/vue-query";
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, queryString } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { formatDate, projectName, shortId } from "../format";
import type { Clarification, Project } from "../types";

const route = useRoute();
const router = useRouter();
const projectId = computed(() => String(route.query.project ?? ""));
const selectedId = computed(() => String(route.query.selected ?? ""));
const clarificationStatus = computed(() => String(route.query.status ?? ""));
const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
const clarifications = useQuery({
  queryKey: computed(() => ["clarifications", projectId.value, clarificationStatus.value]),
  queryFn: () =>
    api<Clarification[]>(
      `/clarifications${queryString({
        project_id: projectId.value,
        status: clarificationStatus.value
      })}`
    ),
  placeholderData: keepPreviousData
});
const selected = computed(() =>
  clarifications.data.value?.find((row) => row.id === selectedId.value)
);

async function selectRow(id: string) {
  await router.replace({ query: { ...route.query, selected: id } });
}

async function closeSelection() {
  const query = { ...route.query };
  delete query.selected;
  await router.replace({ query });
}

async function filterStatus(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  const query = { ...route.query };
  if (value) query.status = value;
  else delete query.status;
  delete query.selected;
  await router.replace({ query });
}
</script>

<template>
  <header class="page-header">
    <div><h1>Уточнения</h1><p>Асинхронные вопросы внешних AI-агентов конкретным людям.</p></div>
    <label class="compact-filter"><span>Статус</span><select :value="clarificationStatus" @change="filterStatus"><option value="">Все</option><option value="pending">Ожидает</option><option value="answered">Отвечено</option><option value="expired">Истекло</option><option value="cancelled">Отменено</option></select></label>
  </header>
  <PageState
    :loading="clarifications.isPending.value"
    :error="clarifications.error.value"
    :empty="clarifications.data.value?.length === 0"
    empty-title="Уточнений по этому фильтру нет"
    @retry="clarifications.refetch()"
  >
    <div class="split-view" :class="{ 'split-view--open': selected }">
      <div class="table-wrap">
        <table class="data-table data-table--selectable">
          <thead><tr><th>Вопрос</th><th>Проект</th><th>Адресат</th><th>Статус</th><th>Создан</th><th>Истекает</th></tr></thead>
          <tbody>
            <tr v-for="row in clarifications.data.value" :key="row.id" :class="{ 'is-selected': row.id === selectedId }" tabindex="0" @click="selectRow(row.id)" @keydown.enter="selectRow(row.id)">
              <td data-label="Вопрос"><strong>{{ row.question }}</strong></td>
              <td data-label="Проект">{{ projectName(projects.data.value, row.project_id) }}</td>
              <td data-label="Адресат"><code>{{ shortId(row.recipient_user_id) }}</code></td>
              <td data-label="Статус"><StatusBadge :value="row.status" /></td>
              <td data-label="Создан">{{ formatDate(row.created_at) }}</td>
              <td data-label="Истекает">{{ formatDate(row.expires_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <aside v-if="selected" class="detail-panel" aria-label="Детали уточнения">
        <button class="detail-panel__close" @click="closeSelection">Закрыть</button>
        <h2>{{ selected.question }}</h2>
        <StatusBadge :value="selected.status" />
        <dl class="detail-list">
          <dt>Контекст</dt><dd>{{ selected.context }}</dd>
          <dt>Ответ</dt><dd>{{ selected.answer || "Ответ ещё не получен" }}</dd>
          <dt>Agent run</dt><dd><code>{{ selected.agent_run_id }}</code></dd>
          <dt>Correlation</dt><dd><code>{{ selected.correlation_id }}</code></dd>
          <dt>Срок</dt><dd>{{ formatDate(selected.expires_at) }}</dd>
        </dl>
      </aside>
    </div>
  </PageState>
</template>
