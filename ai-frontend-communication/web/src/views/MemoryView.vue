<script setup lang="ts">
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, queryString } from "../api";
import PageState from "../components/PageState.vue";
import { formatDate, projectName, shortId } from "../format";
import type { ConversationDetail, ConversationMessage, ConversationSummary, Project } from "../types";

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const projectId = computed(() => String(route.query.project ?? ""));
const selectedId = computed(() => String(route.query.selected ?? ""));
const confirmDelete = ref(false);
const roleLabels: Record<string, string> = {
  user: "Пользователь",
  assistant: "Агент",
  agent: "MCP-агент",
  tool: "Инструмент"
};

const projects = useQuery({
  queryKey: ["projects"],
  queryFn: () => api<Project[]>("/projects"),
  staleTime: 300_000
});

const conversations = useQuery({
  queryKey: computed(() => ["conversations", projectId.value]),
  queryFn: () =>
    api<ConversationSummary[]>(
      `/conversations${queryString({ project_id: projectId.value, limit: "100" })}`
    ),
  placeholderData: keepPreviousData
});

const conversation = useQuery({
  queryKey: computed(() => ["conversation", selectedId.value]),
  queryFn: () => api<ConversationDetail>(`/conversations/${selectedId.value}`),
  enabled: computed(() => Boolean(selectedId.value))
});

watch(selectedId, () => {
  confirmDelete.value = false;
});

const removeConversation = useMutation({
  mutationFn: (id: string) => api<void>(`/conversations/${id}`, { method: "DELETE" }),
  onSuccess: async (_, id) => {
    queryClient.removeQueries({ queryKey: ["conversation", id] });
    await queryClient.invalidateQueries({ queryKey: ["conversations"] });
    await closeSelection();
  }
});

function participant(row: ConversationSummary): string {
  if (row.user_display_name) return row.user_display_name;
  if (row.user_id) return `Пользователь ${shortId(row.user_id)}`;
  if (row.chat_id !== null) return `Чат ${shortId(row.chat_id)}`;
  return "Системный диалог";
}

function roleLabel(role: string): string {
  return roleLabels[role] ?? role;
}

function roleClass(message: ConversationMessage): string {
  if (message.role === "assistant") return "conversation-message--assistant";
  if (message.role === "agent" || message.role === "tool") return "conversation-message--system";
  return "conversation-message--user";
}

async function selectConversation(id: string) {
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
      <span class="eyebrow">Контекст автономного агента</span>
      <h1>Память</h1>
      <p>История диалогов и устойчивые факты, которые Claude использует в следующих ответах.</p>
    </div>
    <button class="button button--secondary button--small" type="button" @click="conversations.refetch()">
      Обновить
    </button>
  </header>

  <PageState
    :loading="conversations.isPending.value"
    :error="conversations.error.value"
    :empty="conversations.data.value?.length === 0"
    empty-title="Диалогов в этом контексте пока нет"
    @retry="conversations.refetch()"
  >
    <div class="memory-layout" :class="{ 'memory-layout--open': selectedId }">
      <section class="conversation-list" aria-label="Диалоги агента">
        <button
          v-for="row in conversations.data.value"
          :key="row.id"
          class="conversation-row"
          :class="{ 'conversation-row--selected': row.id === selectedId }"
          :aria-pressed="row.id === selectedId"
          type="button"
          @click="selectConversation(row.id)"
        >
          <span class="conversation-row__main">
            <span class="conversation-row__meta">
              <span>{{ projectName(projects.data.value, row.project_id) }}</span>
              <span>{{ formatDate(row.last_message_at) }}</span>
            </span>
            <strong>{{ participant(row) }}</strong>
            <small>
              {{ row.chat_id !== null ? `Контекст чата · ${shortId(row.chat_id)}` : "Личный контекст" }}
            </small>
          </span>
          <span class="conversation-row__counts">
            <strong>{{ row.message_count }}</strong><small>сообщений</small>
            <strong>{{ row.memory_count }}</strong><small>фактов</small>
          </span>
        </button>
      </section>

      <aside v-if="selectedId" class="memory-detail" aria-label="Содержимое памяти">
        <button class="detail-panel__close" type="button" @click="closeSelection">Закрыть</button>
        <PageState :loading="conversation.isPending.value" :error="conversation.error.value" @retry="conversation.refetch()">
          <template v-if="conversation.data.value">
            <header class="memory-detail__header">
              <span class="eyebrow">{{ projectName(projects.data.value, conversation.data.value.project_id) }}</span>
              <h2>{{ participant(conversation.data.value) }}</h2>
              <p>Обновлён {{ formatDate(conversation.data.value.updated_at) }}</p>
            </header>

            <section class="memory-section">
              <div class="run-section__header">
                <h3>Сохранённые факты</h3>
                <span>{{ conversation.data.value.memories.length }}</span>
              </div>
              <div v-if="conversation.data.value.memories.length" class="memory-records">
                <article v-for="memory in conversation.data.value.memories" :key="memory.id" class="memory-record">
                  <header><span>{{ memory.kind }}</span><code>{{ memory.memory_key }}</code></header>
                  <p>{{ memory.content }}</p>
                  <small>Обновлено {{ formatDate(memory.updated_at) }}</small>
                </article>
              </div>
              <p v-else class="muted-note">Устойчивые факты ещё не выделены.</p>
            </section>

            <section class="memory-section">
              <div class="run-section__header">
                <h3>История сообщений</h3>
                <span>{{ conversation.data.value.messages.length }}</span>
              </div>
              <ol v-if="conversation.data.value.messages.length" class="conversation-timeline">
                <li v-for="message in conversation.data.value.messages" :key="message.id" class="conversation-message" :class="roleClass(message)">
                  <header>
                    <strong>{{ roleLabel(message.role) }}</strong>
                    <span>{{ message.source }} · {{ formatDate(message.created_at) }}</span>
                  </header>
                  <p>{{ message.content }}</p>
                </li>
              </ol>
              <p v-else class="muted-note">Сообщений в диалоге нет.</p>
            </section>

            <div class="danger-row memory-delete">
              <template v-if="confirmDelete">
                <span>Удалить весь диалог и сохранённые факты? Это действие необратимо.</span>
                <button class="button button--danger button--small" type="button" :disabled="removeConversation.isPending.value" @click="removeConversation.mutate(conversation.data.value.id)">
                  {{ removeConversation.isPending.value ? "Удаляю…" : "Удалить память" }}
                </button>
                <button class="button button--ghost button--small" type="button" :disabled="removeConversation.isPending.value" @click="confirmDelete = false">Отмена</button>
              </template>
              <button v-else class="text-button text-button--danger" type="button" @click="confirmDelete = true">Удалить диалог</button>
            </div>
            <p v-if="removeConversation.error.value" class="inline-error" role="alert">{{ removeConversation.error.value.message }}</p>
          </template>
        </PageState>
      </aside>
    </div>
  </PageState>
</template>
