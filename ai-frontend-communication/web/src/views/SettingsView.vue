<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";

import { api } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import type { Project, ReadyStatus } from "../types";

const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
const readiness = useQuery({
  queryKey: ["readiness", "deep"],
  queryFn: () => api<ReadyStatus>("/health/ready?deep=true"),
  refetchInterval: 30_000,
  retry: false
});
</script>

<template>
  <header class="page-header"><div><h1>Настройки</h1><p>Проекты и read-only состояние системных интеграций.</p></div></header>
  <section class="section-block"><h2>Проекты</h2><PageState :loading="projects.isPending.value" :error="projects.error.value" :empty="projects.data.value?.length === 0" @retry="projects.refetch()"><div class="table-wrap"><table class="data-table"><thead><tr><th>Название</th><th>Slug</th><th>Состояние</th></tr></thead><tbody><tr v-for="project in projects.data.value" :key="project.id"><td data-label="Название"><strong>{{ project.name }}</strong></td><td data-label="Slug"><code>{{ project.slug }}</code></td><td data-label="Состояние"><StatusBadge :value="project.enabled ? 'ready' : 'disabled'" /></td></tr></tbody></table></div></PageState></section>
  <section class="section-block"><h2>Система</h2><PageState :loading="readiness.isPending.value" :error="readiness.error.value" @retry="readiness.refetch()"><dl class="system-status"><div><dt>PostgreSQL</dt><dd><StatusBadge :value="readiness.data.value?.checks.database ? 'ready' : 'failed'" /></dd></div><div><dt>Redis</dt><dd><StatusBadge :value="readiness.data.value?.checks.redis ? 'ready' : 'failed'" /></dd></div><div><dt>Telegram</dt><dd><StatusBadge :value="readiness.data.value?.checks.telegram?.ok ? 'ready' : 'failed'" /></dd></div><div><dt>Private topics</dt><dd>{{ readiness.data.value?.checks.telegram?.has_topics_enabled ? "Включены" : "Не подтверждены" }}</dd></div><div><dt>Guest Mode</dt><dd>{{ readiness.data.value?.checks.telegram?.supports_guest_queries ? "Поддерживается" : "Не подтверждён" }}</dd></div></dl><p class="muted-note">Секреты, deploy keys и service tokens здесь намеренно не отображаются.</p></PageState></section>
</template>

