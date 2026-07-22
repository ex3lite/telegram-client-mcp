<script setup lang="ts">
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";

import { api } from "./api";
import type { AdminIdentity, Project } from "./types";

const route = useRoute();
const router = useRouter();
const queryClient = useQueryClient();
const online = ref(navigator.onLine);
const isLogin = computed(() => route.name === "login");
const selectedProject = computed(() => String(route.query.project ?? ""));

const identity = useQuery({
  queryKey: ["auth", "me"],
  queryFn: () => api<AdminIdentity>("/auth/me"),
  enabled: computed(() => !isLogin.value),
  staleTime: Number.POSITIVE_INFINITY
});

const projects = useQuery({
  queryKey: ["projects"],
  queryFn: () => api<Project[]>("/projects"),
  enabled: computed(() => !isLogin.value),
  staleTime: 300_000
});

const navigation = [
  { name: "overview", label: "Центр", index: "01" },
  { name: "runs", label: "Запуски", index: "02" },
  { name: "agent", label: "Агент", index: "03" },
  { name: "memory", label: "Память", index: "04" },
  { name: "mcp", label: "MCP", index: "05" },
  { name: "repositories", label: "Репозитории", index: "06" }
] as const;

const workNavigation = [
  { name: "requests", label: "Заявки" },
  { name: "members", label: "Участники" },
  { name: "clarifications", label: "Уточнения" }
] as const;

async function chooseProject(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  const query = { ...route.query };
  delete query.selected;
  if (value) query.project = value;
  else delete query.project;
  await router.replace({ query });
}

async function logout() {
  await api<void>("/auth/logout", { method: "POST" });
  queryClient.removeQueries({ queryKey: ["auth", "me"] });
  await router.push({ name: "login" });
}

function updateOnline() {
  online.value = navigator.onLine;
}

onMounted(() => {
  window.addEventListener("online", updateOnline);
  window.addEventListener("offline", updateOnline);
});

onBeforeUnmount(() => {
  window.removeEventListener("online", updateOnline);
  window.removeEventListener("offline", updateOnline);
});
</script>

<template>
  <RouterView v-if="isLogin" />
  <div v-else class="app-shell">
    <a class="skip-link" href="#main-content">Перейти к содержимому</a>
    <aside class="sidebar" aria-label="Основная навигация">
      <div class="product-mark">
        <span class="product-mark__glyph" aria-hidden="true">KA</span>
        <span>
          <strong>Kakadu Agency</strong>
          <small>Agent control plane</small>
        </span>
      </div>

      <nav class="primary-nav">
        <RouterLink
          v-for="item in navigation"
          :key="item.name"
          :to="{ name: item.name, query: { project: route.query.project } }"
          class="nav-link"
          :aria-current="route.name === item.name ? 'page' : undefined"
        >
          <span class="nav-link__index">{{ item.index }}</span>
          <span>{{ item.label }}</span>
        </RouterLink>

        <div class="nav-group">
          <span class="nav-group__label">Работа</span>
          <RouterLink
            v-for="item in workNavigation"
            :key="item.name"
            :to="{ name: item.name, query: { project: route.query.project } }"
            class="nav-link nav-link--nested"
            :aria-current="route.name === item.name ? 'page' : undefined"
          >
            {{ item.label }}
          </RouterLink>
        </div>

        <RouterLink
          :to="{ name: 'audit', query: { project: route.query.project } }"
          class="nav-link nav-link--audit"
          :aria-current="route.name === 'audit' ? 'page' : undefined"
        >
          <span class="nav-link__index">07</span>
          <span>Аудит</span>
        </RouterLink>
      </nav>

      <div class="sidebar__footer">
        <span class="connection-dot" :class="{ 'connection-dot--offline': !online }"></span>
        {{ online ? "Система на связи" : "Нет соединения" }}
      </div>
    </aside>

    <div class="workspace">
      <div v-if="!online" class="offline-banner" role="status">
        Нет соединения. Показаны последние полученные данные.
      </div>
      <header class="topbar">
        <label class="project-picker">
          <span>Контекст проекта</span>
          <select :value="selectedProject" @change="chooseProject">
            <option value="">Все проекты</option>
            <option v-for="project in projects.data.value" :key="project.id" :value="project.id">
              {{ project.name }}
            </option>
          </select>
        </label>
        <div class="operator-menu">
          <span class="operator-menu__identity">
            <small>Администратор</small>
            <strong>{{ identity.data.value?.name ?? "Загрузка…" }}</strong>
          </span>
          <button class="button button--ghost button--small" type="button" @click="logout">
            Выйти
          </button>
        </div>
      </header>
      <main id="main-content" class="main-content" tabindex="-1">
        <RouterView />
      </main>
    </div>
  </div>
</template>
