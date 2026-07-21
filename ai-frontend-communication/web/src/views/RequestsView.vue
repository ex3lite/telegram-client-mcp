<script setup lang="ts">
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
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
  refetchInterval: 15_000
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
              <td data-label="Тип">{{ row.kind }}</td>
              <td data-label="Приоритет">{{ row.priority }}</td>
              <td data-label="Статус"><StatusBadge :value="row.status" /></td>
              <td data-label="Создана">{{ formatDate(row.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <aside v-if="selected" class="detail-panel" aria-label="Детали заявки">
        <button class="detail-panel__close" type="button" @click="closeSelection">Закрыть</button>
        <h2>{{ selected.title }}</h2>
        <StatusBadge :value="selected.status" />
        <dl class="detail-list">
          <dt>ID</dt><dd><code>{{ selected.id }}</code></dd>
          <dt>Correlation</dt><dd><code>{{ selected.correlation_id }}</code></dd>
          <dt>Источник</dt><dd>{{ selected.source }}</dd>
          <dt>Описание</dt><dd>{{ selected.description || "Не указано" }}</dd>
          <dt>Создана</dt><dd>{{ formatDate(selected.created_at) }}</dd>
        </dl>
        <div v-if="targets[selected.status].length" class="detail-actions">
          <button
            v-for="target in targets[selected.status]"
            :key="target"
            class="cds--btn cds--btn--sm cds--btn--tertiary"
            :disabled="changeStatus.isPending.value"
            @click="mutateStatus(selected, target)"
          >
            {{ target }}
          </button>
        </div>
      </aside>
    </div>
  </PageState>
</template>
