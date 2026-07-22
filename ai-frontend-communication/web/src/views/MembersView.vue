<script setup lang="ts">
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, queryString } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { projectName } from "../format";
import type {
  MemberKnowledgeScope,
  MemberLanguage,
  Project,
  ProjectMember
} from "../types";

interface MemberDraft {
  display_name: string;
  telegram_user_id: number | null;
  telegram_username: string;
  role: string;
  department: string;
  stack: string;
  language: MemberLanguage;
  knowledge_scope: MemberKnowledgeScope;
  can_create_requests: boolean;
  active: boolean;
}

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const projectId = computed(() => String(route.query.project ?? ""));
const selectedKey = computed(() => String(route.query.selected ?? ""));
const saved = ref(false);
const draft = reactive<MemberDraft>({
  display_name: "",
  telegram_user_id: null,
  telegram_username: "",
  role: "developer",
  department: "",
  stack: "",
  language: "ru",
  knowledge_scope: "integration",
  can_create_requests: true,
  active: true
});

const projects = useQuery({
  queryKey: ["projects"],
  queryFn: () => api<Project[]>("/projects"),
  staleTime: 300_000
});

const members = useQuery({
  queryKey: computed(() => ["members", projectId.value]),
  queryFn: () =>
    api<ProjectMember[]>(`/members${queryString({ project_id: projectId.value || undefined })}`),
  placeholderData: keepPreviousData
});

function memberKey(member: ProjectMember): string {
  return `${member.project_id}:${member.user_id}`;
}

const selected = computed(() =>
  members.data.value?.find((member) => memberKey(member) === selectedKey.value)
);

watch(
  selected,
  (member) => {
    saved.value = false;
    if (!member) return;
    Object.assign(draft, {
      display_name: member.display_name,
      telegram_user_id: member.telegram_user_id,
      telegram_username: member.telegram_username ?? "",
      role: member.role,
      department: member.department ?? "",
      stack: member.stack ?? "",
      language: member.language,
      knowledge_scope: member.knowledge_scope,
      can_create_requests: member.can_create_requests,
      active: member.active
    });
  },
  { immediate: true }
);

const updateMember = useMutation({
  mutationFn: (member: ProjectMember) =>
    api<ProjectMember>(`/projects/${member.project_id}/members/${member.user_id}`, {
      method: "PUT",
      body: JSON.stringify({
        ...draft,
        telegram_username: draft.telegram_username || null,
        department: draft.department || null,
        stack: draft.stack || null
      })
    }),
  onSuccess: (updated) => {
    queryClient.setQueryData<ProjectMember[]>(
      ["members", projectId.value],
      (rows) => rows?.map((row) => (memberKey(row) === memberKey(updated) ? updated : row)) ?? []
    );
    saved.value = true;
  }
});

async function selectMember(member: ProjectMember) {
  await router.replace({ query: { ...route.query, selected: memberKey(member) } });
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
      <span class="eyebrow">Контекст и права агента</span>
      <h1>Участники</h1>
      <p>Кто пишет боту, на каком языке отвечать и какой уровень знаний можно раскрывать.</p>
    </div>
    <button class="button button--secondary button--small" type="button" @click="members.refetch()">
      Обновить
    </button>
  </header>

  <PageState
    :loading="members.isPending.value"
    :error="members.error.value"
    :empty="members.data.value?.length === 0"
    empty-title="В выбранном проекте нет участников"
    @retry="members.refetch()"
  >
    <div class="split-view members-layout" :class="{ 'split-view--open': selected }">
      <div class="table-wrap">
        <table class="data-table data-table--selectable">
          <thead>
            <tr>
              <th>Участник</th>
              <th>Проект</th>
              <th>Telegram</th>
              <th>Роль и стек</th>
              <th>Доступ</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="member in members.data.value"
              :key="memberKey(member)"
              :class="{ 'is-selected': memberKey(member) === selectedKey }"
              tabindex="0"
              @click="selectMember(member)"
              @keydown.enter="selectMember(member)"
            >
              <td data-label="Участник"><strong>{{ member.display_name }}</strong><small>{{ member.language === "ru" ? "Русский" : "English" }}</small></td>
              <td data-label="Проект">{{ projectName(projects.data.value, member.project_id) }}</td>
              <td data-label="Telegram"><strong>{{ member.telegram_username ? `@${member.telegram_username}` : "Без username" }}</strong><small>{{ member.telegram_user_id ?? "ID не привязан" }}</small></td>
              <td data-label="Роль и стек"><strong>{{ member.role }}</strong><small>{{ [member.department, member.stack].filter(Boolean).join(" · ") || "Не указано" }}</small></td>
              <td data-label="Доступ">{{ member.knowledge_scope === "internal" ? "Внутренний" : "Только интеграция" }}<small>{{ member.can_create_requests ? "Может создавать заявки" : "Без заявок" }}</small></td>
              <td data-label="Статус"><StatusBadge :value="member.active ? 'active' : 'disabled'" /><small>{{ member.telegram_reachable ? "Бот может написать" : member.telegram_verified ? "Чат недоступен" : "Telegram не подтверждён" }}</small></td>
            </tr>
          </tbody>
        </table>
      </div>

      <aside v-if="selected" class="detail-panel member-editor" aria-label="Редактирование участника">
        <button class="detail-panel__close" type="button" @click="closeSelection">Закрыть</button>
        <span class="eyebrow">Профиль для Claude</span>
        <h2>{{ selected.display_name }}</h2>
        <p class="member-editor__intro">Эти поля формируют доверенный контекст и права участника.</p>

        <form class="member-form" @submit.prevent="updateMember.mutate(selected)">
          <label class="field"><span>Имя</span><input v-model.trim="draft.display_name" maxlength="160" required /></label>
          <div class="member-form__row">
            <label class="field"><span>Telegram ID</span><input v-model.number="draft.telegram_user_id" type="number" min="1" required /></label>
            <label class="field"><span>Username</span><input v-model.trim="draft.telegram_username" maxlength="64" placeholder="без @" /></label>
          </div>
          <label class="field"><span>Роль</span><input v-model.trim="draft.role" maxlength="40" placeholder="android, ios, web, backend" required /></label>
          <label class="field"><span>Отдел</span><input v-model.trim="draft.department" maxlength="80" placeholder="Mobile, Frontend, Backend" /></label>
          <label class="field"><span>Стек</span><input v-model.trim="draft.stack" maxlength="160" placeholder="Kotlin, Swift, JavaScript" /></label>

          <div class="member-form__row">
            <label class="field">
              <span>Язык ответа</span>
              <select v-model="draft.language">
                <option value="ru">Русский</option>
                <option value="en">English</option>
              </select>
            </label>
            <label class="field">
              <span>Что можно раскрывать</span>
              <select v-model="draft.knowledge_scope">
                <option value="integration">Контракт интеграции</option>
                <option value="internal">Внутреннее устройство</option>
              </select>
            </label>
          </div>

          <label class="member-permission">
            <input v-model="draft.can_create_requests" type="checkbox" />
            <span><strong>Разрешить создавать заявки</strong><small>Агент сможет положить запрос участника в ящик backend-команды.</small></span>
          </label>
          <label class="member-permission">
            <input v-model="draft.active" type="checkbox" />
            <span><strong>Доступ к боту (whitelist)</strong><small>Если выключить, Братулец сразу перестанет принимать сообщения этого Telegram-пользователя.</small></span>
          </label>

          <p v-if="updateMember.error.value" class="inline-error" role="alert">Не удалось сохранить: {{ updateMember.error.value.message }}</p>
          <p v-if="saved" class="inline-success" role="status">Профиль сохранён.</p>
          <button class="button button--primary" type="submit" :disabled="updateMember.isPending.value || !draft.telegram_user_id">
            {{ updateMember.isPending.value ? "Сохраняю…" : "Сохранить профиль" }}
          </button>
        </form>
      </aside>
    </div>
  </PageState>
</template>

<style scoped>
.members-layout.split-view--open {
  grid-template-columns: minmax(42rem, 1fr) minmax(24rem, 30rem);
}

.member-editor__intro {
  margin: -0.35rem 0 1.35rem;
  color: var(--muted);
  font-size: 0.84rem;
}

.member-form {
  display: grid;
  gap: 0.9rem;
}

.member-form__row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.member-permission {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: start;
  gap: 0.65rem;
  padding: 0.8rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: #f8f5ee;
  cursor: pointer;
}

.member-permission input {
  margin-top: 0.2rem;
}

.member-permission strong,
.member-permission small {
  display: block;
}

.member-permission small {
  margin-top: 0.2rem;
  color: var(--muted);
  line-height: 1.4;
}

@media (max-width: 82rem) {
  .members-layout.split-view--open {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 42rem) {
  .member-form__row {
    grid-template-columns: 1fr;
  }
}
</style>
