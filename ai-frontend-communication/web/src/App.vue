<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";

import { api } from "./api";
import type { Project } from "./types";

const route = useRoute();
const router = useRouter();
const online = ref(navigator.onLine);
const isLogin = computed(() => route.name === "login");
const selectedProject = computed(() => String(route.query.project ?? ""));

const projects = useQuery({
  queryKey: ["projects"],
  queryFn: () => api<Project[]>("/projects"),
  enabled: computed(() => !isLogin.value),
  staleTime: 30_000
});

const navigation = [
  ["overview", "Обзор"],
  ["requests", "Заявки"],
  ["clarifications", "Уточнения"],
  ["repositories", "Репозитории"],
  ["audit", "Аудит"],
  ["settings", "Настройки"]
] as const;

async function chooseProject(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  const query = { ...route.query };
  if (value) query.project = value;
  else delete query.project;
  await router.replace({ query });
}

async function logout() {
  await api<void>("/auth/logout", { method: "POST" });
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
        <span class="product-mark__glyph" aria-hidden="true">DC</span>
        <span>
          <strong>Developer Agent</strong>
          <small>Operations</small>
        </span>
      </div>
      <nav>
        <RouterLink
          v-for="[name, label] in navigation"
          :key="name"
          :to="{ name, query: route.query }"
          class="nav-link"
          :aria-current="route.name === name ? 'page' : undefined"
        >
          {{ label }}
        </RouterLink>
      </nav>
    </aside>
    <div class="workspace">
      <div v-if="!online" class="offline-banner" role="status">
        Нет соединения. Показаны последние полученные данные.
      </div>
      <header class="topbar">
        <label class="project-picker">
          <span>Проект</span>
          <select :value="selectedProject" @change="chooseProject">
            <option value="">Все проекты</option>
            <option v-for="project in projects.data.value" :key="project.id" :value="project.id">
              {{ project.name }}
            </option>
          </select>
        </label>
        <button class="text-button" type="button" @click="logout">Выйти</button>
      </header>
      <main id="main-content" class="main-content" tabindex="-1">
        <RouterView />
      </main>
    </div>
  </div>
</template>

