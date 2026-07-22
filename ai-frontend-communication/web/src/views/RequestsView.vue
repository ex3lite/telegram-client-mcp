<script setup lang="ts">
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, queryString } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { formatDate, projectName, shortId } from "../format";
import type { ChangeRequest, Project } from "../types";

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const projectId = computed(() => String(route.query.project ?? ""));
const selectedId = computed(() => String(route.query.selected ?? ""));
const requestStatus = computed(() => String(route.query.status ?? ""));

const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
const requests = useQuery({
  queryKey: computed(() => ["requests", projectId.value, requestStatus.value]),
  queryFn: () =>
    api<ChangeRequest[]>(
      `/requests${queryString({ project_id: projectId.value, status: requestStatus.value })}`
    ),
  placeholderData: keepPreviousData
});
const selected = computed(() => requests.data.value?.find((row) => row.id === selectedId.value));

const changeStatus = useMutation({
  mutationFn: ({ row, status }: { row: ChangeRequest; status: ChangeRequest["status"] }) =>
    api<ChangeRequest>(`/requests/${row.id}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status, expected_version: row.version })
    }),
  onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["requests"] })
});

const targets: Record<ChangeRequest["status"], ChangeRequest["status"][]> = {
  open: ["in_progress", "rejected"],
  in_progress: ["done", "rejected"],
  done: [],
  rejected: []
};
const statusLabels: Record<ChangeRequest["status"], string> = {
  open: "Открыть",
  in_progress: "Взять в работу",
  done: "Завершить",
  rejected: "Отклонить"
};
const kindLabels: Record<ChangeRequest["kind"], string> = {
  bug: "Ошибка",
  task: "Задача",
  feature: "Новая функция",
  integration: "Интеграция",
  change: "Доработка",
  question: "Нужен ответ Backend"
};
const priorityLabels: Record<ChangeRequest["priority"], string> = {
  low: "Низкий",
  normal: "Обычный",
  high: "Высокий",
  urgent: "Срочный"
};

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

function mutateStatus(row: ChangeRequest, status: ChangeRequest["status"]) {
  if (!window.confirm(`Изменить статус заявки на «${status}»?`)) return;
  changeStatus.mutate({ row, status });
}
</script>

<template>
  <header class="page-header">
    <div><h1>Заявки</h1><p>Изменения, созданные в Telegram и внутренних инструментах.</p></div>
    <label class="compact-filter">
      <span>Статус</span>
      <select :value="requestStatus" @change="filterStatus">
        <option value="">Все</option>
        <option value="open">Открыта</option>
        <option value="in_progress">В работе</option>
        <option value="done">Готово</option>
        <option value="rejected">Отклонена</option>
      </select>
    </label>
  </header>
  <p v-if="changeStatus.error.value" class="inline-error" role="alert">
    Не удалось изменить статус: {{ changeStatus.error.value.message }}
  </p>
  <PageState
    :loading="requests.isPending.value"
    :error="requests.error.value"
    :empty="requests.data.value?.length === 0"
    empty-title="Заявок по этому фильтру нет"
    @retry="requests.refetch()"
  >
    <div class="split-view" :class="{ 'split-view--open': selected }">
      <div class="table-wrap">
        <table class="data-table data-table--selectable">
          <thead><tr><th>ID</th><th>Заголовок</th><th>Проект</th><th>Тип</th><th>Приоритет</th><th>Статус</th><th>Создана</th></tr></thead>
          <tbody>
            <tr
              v-for="row in requests.data.value"
              :key="row.id"
              :class="{ 'is-selected': row.id === selectedId }"
              tabindex="0"
              @click="selectRow(row.id)"
              @keydown.enter="selectRow(row.id)"
            >
              <td data-label="ID"><code>{{ shortId(row.id) }}</code></td>
              <td data-label="Заголовок"><strong>{{ row.title }}</strong></td>
              <td data-label="Проект">{{ projectName(projects.data.value, row.project_id) }}</td>
              <td data-label="Тип">{{ kindLabels[row.kind] }}</td>
              <td data-label="Приоритет">{{ priorityLabels[row.priority] }}</td>
              <td data-label="Статус"><StatusBadge :value="row.status" /></td>
              <td data-label="Создана">{{ formatDate(row.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <aside v-if="selected" class="detail-panel request-detail" aria-label="Детали заявки">
        <button class="detail-panel__close" type="button" @click="closeSelection">Закрыть</button>
        <h2>{{ selected.title }}</h2>
        <StatusBadge :value="selected.status" />
        <dl class="detail-list">
          <dt>ID</dt><dd><code>{{ selected.id }}</code></dd>
          <dt>Проект</dt><dd>{{ projectName(projects.data.value, selected.project_id) }}</dd>
          <dt>Тип</dt><dd>{{ kindLabels[selected.kind] }}</dd>
          <dt>Приоритет</dt><dd>{{ priorityLabels[selected.priority] }}</dd>
          <dt>Автор</dt><dd>{{ selected.requester_profile.display_name || "Системный запрос" }}</dd>
          <dt>Профиль</dt>
          <dd>
            {{ [selected.requester_profile.role, selected.requester_profile.department, selected.requester_profile.stack].filter(Boolean).join(" · ") || "Не указан" }}
            <br v-if="selected.requester_profile.language" />
            <small v-if="selected.requester_profile.language">Язык: {{ selected.requester_profile.language }}</small>
          </dd>
          <dt>Correlation</dt><dd><code>{{ selected.correlation_id }}</code></dd>
          <dt>Источник</dt><dd>{{ selected.source }}</dd>
          <dt v-if="selected.source_interaction_id">Запуск агента</dt>
          <dd v-if="selected.source_interaction_id">
            <RouterLink :to="{ name: 'runs', query: { ...route.query, selected: selected.source_interaction_id } }">
              Открыть исходный запуск
            </RouterLink>
          </dd>
          <dt>Создана</dt><dd>{{ formatDate(selected.created_at) }}</dd>
          <dt>Обновлена</dt><dd>{{ formatDate(selected.updated_at) }}</dd>
        </dl>
        <section v-if="selected.question" class="run-section">
          <h3>Исходный вопрос</h3>
          <p class="markdown-output">{{ selected.question }}</p>
        </section>
        <section v-if="selected.agent_summary || selected.description" class="run-section">
          <h3>{{ selected.agent_summary ? "Резюме агента" : "Описание" }}</h3>
          <p class="markdown-output">{{ selected.agent_summary || selected.description }}</p>
        </section>
        <section v-if="selected.citations.length" class="run-section">
          <div class="run-section__header">
            <h3>Подтверждённые источники</h3>
            <span>{{ selected.citations.length }}</span>
          </div>
          <ul class="citation-list">
            <li v-for="citation in selected.citations" :key="`${citation.path}:${citation.start_line}`">
              <code>{{ citation.path }}:{{ citation.start_line }}–{{ citation.end_line }}</code>
            </li>
          </ul>
        </section>
        <div v-if="targets[selected.status].length" class="detail-actions">
          <button
            v-for="target in targets[selected.status]"
            :key="target"
            class="button button--secondary button--small"
            :disabled="changeStatus.isPending.value"
            @click="mutateStatus(selected, target)"
          >
            {{ statusLabels[target] }}
          </button>
        </div>
      </aside>
    </div>
  </PageState>
</template>
