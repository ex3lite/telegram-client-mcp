<script setup lang="ts">
import { keepPreviousData, useQuery } from "@tanstack/vue-query";
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api, queryString } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { formatDate, shortId } from "../format";
import type { Overview } from "../types";

const route = useRoute();
const projectId = computed(() => String(route.query.project ?? ""));
const overview = useQuery({
  queryKey: computed(() => ["overview", projectId.value]),
  queryFn: () => api<Overview>(`/overview${queryString({ project_id: projectId.value })}`),
  placeholderData: keepPreviousData,
  refetchInterval: 30_000
});
</script>

<template>
  <header class="page-header">
    <div>
      <span class="eyebrow">Состояние автономной системы</span>
      <h1>Центр управления</h1>
      <p>Очередь решений, живые запуски и быстрый доступ к настройкам агента.</p>
    </div>
    <button class="button button--secondary button--small" @click="overview.refetch()">
      Обновить
    </button>
  </header>
  <PageState
    :loading="overview.isPending.value"
    :error="overview.error.value"
    @retry="overview.refetch()"
  >
    <section class="attention-grid" aria-label="Сводка очереди">
      <RouterLink :to="{ name: 'requests', query: route.query }">
        <strong>{{ overview.data.value?.attention.open_requests ?? 0 }}</strong>
        <span>Открытые заявки</span>
      </RouterLink>
      <RouterLink :to="{ name: 'clarifications', query: route.query }">
        <strong>{{ overview.data.value?.attention.pending_clarifications ?? 0 }}</strong>
        <span>Ожидают ответа</span>
      </RouterLink>
      <RouterLink :to="{ name: 'repositories', query: route.query }">
        <strong>{{ overview.data.value?.attention.repository_errors ?? 0 }}</strong>
        <span>Ошибки репозиториев</span>
      </RouterLink>
      <RouterLink :to="{ name: 'audit', query: route.query }">
        <strong>{{ overview.data.value?.attention.delivery_uncertain ?? 0 }}</strong>
        <span>Неопределённые доставки</span>
      </RouterLink>
    </section>
    <section class="control-links" aria-label="Быстрые действия">
      <RouterLink :to="{ name: 'runs', query: { project: route.query.project } }">
        <span class="control-links__index">A</span>
        <span><strong>Открыть запуски</strong><small>Ответы Claude, артефакты и privacy findings</small></span>
        <span aria-hidden="true">→</span>
      </RouterLink>
      <RouterLink :to="{ name: 'agent', query: { project: route.query.project } }">
        <span class="control-links__index">B</span>
        <span><strong>Настроить агента</strong><small>Модель, промпт, бюджет и Telegram</small></span>
        <span aria-hidden="true">→</span>
      </RouterLink>
      <RouterLink :to="{ name: 'mcp', query: { project: route.query.project } }">
        <span class="control-links__index">C</span>
        <span><strong>Управлять MCP</strong><small>Токены, scopes и доступ к проектам</small></span>
        <span aria-hidden="true">→</span>
      </RouterLink>
    </section>
    <section class="section-block">
      <div class="section-heading"><div><span class="eyebrow">Журнал</span><h2>Последние события</h2></div><RouterLink :to="{ name: 'audit', query: { project: route.query.project } }">Весь аудит →</RouterLink></div>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th scope="col">Время</th>
              <th scope="col">Событие</th>
              <th scope="col">Actor</th>
              <th scope="col">Результат</th>
              <th scope="col">Correlation</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="event in overview.data.value?.recent_events" :key="event.id">
              <td data-label="Время">{{ formatDate(event.occurred_at) }}</td>
              <td data-label="Событие"><strong>{{ event.event_type }}</strong></td>
              <td data-label="Actor">{{ event.actor.type }}:{{ shortId(event.actor.id) }}</td>
              <td data-label="Результат"><StatusBadge :value="event.outcome" /></td>
              <td data-label="Correlation"><code>{{ shortId(event.correlation_id) }}</code></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </PageState>
</template>
