<script setup lang="ts">
import { keepPreviousData, useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, queryString } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { formatDate, projectName, shortId } from "../format";
import type { AuditEvent, Project } from "../types";

const route = useRoute();
const router = useRouter();
const projectId = computed(() => String(route.query.project ?? ""));
const eventType = ref(String(route.query.event_type ?? ""));
const correlation = ref(String(route.query.correlation_id ?? ""));
const selectedId = computed(() => String(route.query.selected ?? ""));
const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
const audit = useQuery({
  queryKey: computed(() => ["audit", projectId.value, route.query.event_type, route.query.correlation_id]),
    queryFn: () => api<AuditEvent[]>(`/audit${queryString({ project_id: projectId.value, event_type: String(route.query.event_type ?? ""), correlation_id: String(route.query.correlation_id ?? "") })}`),
  placeholderData: keepPreviousData
});
const selected = computed(() => audit.data.value?.find((row) => row.id === selectedId.value));

async function applyFilters() {
  const query = { ...route.query };
  if (eventType.value) query.event_type = eventType.value; else delete query.event_type;
  if (correlation.value) query.correlation_id = correlation.value; else delete query.correlation_id;
  delete query.selected;
  await router.replace({ query });
}

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
  <header class="page-header"><div><h1>Аудит</h1><p>Неизменяемая цепочка решений и внешних действий.</p></div></header>
  <form class="filter-bar" @submit.prevent="applyFilters"><label><span>Тип события</span><input v-model="eventType" placeholder="clarification.answered" /></label><label><span>Correlation ID</span><input v-model="correlation" placeholder="corr-..." /></label><button class="button button--primary button--small">Применить</button></form>
  <PageState :loading="audit.isPending.value" :error="audit.error.value" :empty="audit.data.value?.length === 0" empty-title="Событий по этому фильтру нет" @retry="audit.refetch()">
    <div class="split-view" :class="{ 'split-view--open': selected }"><div class="table-wrap"><table class="data-table data-table--selectable"><thead><tr><th>Время</th><th>Событие</th><th>Проект</th><th>Actor</th><th>Результат</th><th>Correlation</th></tr></thead><tbody><tr v-for="row in audit.data.value" :key="row.id" :class="{ 'is-selected': row.id === selectedId }" tabindex="0" @click="selectRow(row.id)" @keydown.enter="selectRow(row.id)"><td data-label="Время">{{ formatDate(row.occurred_at) }}</td><td data-label="Событие"><strong>{{ row.event_type }}</strong></td><td data-label="Проект">{{ projectName(projects.data.value, row.project_id) }}</td><td data-label="Actor">{{ row.actor.type }}:{{ shortId(row.actor.id) }}</td><td data-label="Результат"><StatusBadge :value="row.outcome" /></td><td data-label="Correlation"><code>{{ shortId(row.correlation_id) }}</code></td></tr></tbody></table></div><aside v-if="selected" class="detail-panel" aria-label="Детали события"><button class="detail-panel__close" type="button" @click="closeSelection">Закрыть</button><h2>{{ selected.event_type }}</h2><StatusBadge :value="selected.outcome" /><dl class="detail-list"><dt>Event ID</dt><dd><code>{{ selected.id }}</code></dd><dt>Correlation</dt><dd><code>{{ selected.correlation_id }}</code></dd><dt>Actor</dt><dd>{{ selected.actor.type }}: {{ selected.actor.id }}</dd><dt>Subject</dt><dd>{{ selected.subject.type }}: {{ selected.subject.id }}</dd><dt>Payload</dt><dd><pre>{{ JSON.stringify(selected.payload, null, 2) }}</pre></dd></dl></aside></div>
  </PageState>
</template>
