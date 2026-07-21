<script setup lang="ts">
import { keepPreviousData, useQuery } from "@tanstack/vue-query";
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, queryString } from "../api";
import PageState from "../components/PageState.vue";
import StatusBadge from "../components/StatusBadge.vue";
import { formatDate, projectName, shortId } from "../format";
import type { Interaction, InteractionSummary, KnowledgeArtifact, Project } from "../types";

const route = useRoute();
const router = useRouter();
const projectId = computed(() => String(route.query.project ?? ""));
const runStatus = computed(() => String(route.query.status ?? ""));
const selectedId = computed(() => String(route.query.selected ?? ""));

const projects = useQuery({
  queryKey: ["projects"],
  queryFn: () => api<Project[]>("/projects"),
  staleTime: 300_000
});

const runs = useQuery({
  queryKey: computed(() => ["interactions", projectId.value, runStatus.value]),
  queryFn: () =>
    api<InteractionSummary[]>(
      `/interactions${queryString({
        project_id: projectId.value,
        status: runStatus.value,
        limit: "50"
      })}`
    ),
  placeholderData: keepPreviousData,
  refetchInterval: 30_000
});

const selected = useQuery({
  queryKey: computed(() => ["interaction", selectedId.value]),
  queryFn: () => api<Interaction>(`/interactions/${selectedId.value}`),
  enabled: computed(() => Boolean(selectedId.value)),
  refetchInterval: 30_000
});

async function selectRun(id: string) {
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

function artifactName(artifact: InteractionSummary["artifacts"][number]): string {
  return artifact.filename ?? artifact.name ?? "artifact";
}

function fullArtifactName(artifact: KnowledgeArtifact): string {
  return artifact.name || artifact.filename || "artifact.md";
}

function downloadArtifact(artifact: KnowledgeArtifact) {
  const url = URL.createObjectURL(new Blob([artifact.content], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = fullArtifactName(artifact);
  link.click();
  URL.revokeObjectURL(url);
}
</script>

<template>
  <header class="page-header">
    <div>
      <span class="eyebrow">Автономная работа</span>
      <h1>Запуски</h1>
      <p>Каждый вопрос, решение Claude, созданные Markdown-файлы и срабатывания privacy policy.</p>
    </div>
    <label class="compact-filter">
      <span>Статус</span>
      <select :value="runStatus" @change="filterStatus">
        <option value="">Все</option>
        <option value="queued">В очереди</option>
        <option value="generating">Claude отвечает</option>
        <option value="answer_ready">Ответ готов</option>
        <option value="published">Опубликован</option>
        <option value="failed">Ошибка</option>
      </select>
    </label>
  </header>

  <PageState :loading="runs.isPending.value" :error="runs.error.value" :empty="runs.data.value?.length === 0" empty-title="Запусков по этому фильтру нет" @retry="runs.refetch()">
    <div class="runs-layout" :class="{ 'runs-layout--open': selectedId }">
      <section class="run-list" aria-label="Список запусков">
        <button
          v-for="run in runs.data.value"
          :key="run.id"
          class="run-row"
          :class="{ 'run-row--selected': run.id === selectedId }"
          type="button"
          @click="selectRun(run.id)"
        >
          <span class="run-row__main">
            <span class="run-row__meta">
              <StatusBadge :value="run.status" />
              <span>{{ projectName(projects.data.value, run.project_id) }}</span>
              <span>{{ formatDate(run.created_at) }}</span>
            </span>
            <strong>{{ run.question }}{{ run.question_truncated ? "…" : "" }}</strong>
            <small>{{ run.source }} · {{ run.commit_sha?.slice(0, 12) ?? "без snapshot" }}</small>
          </span>
          <span class="run-row__signals">
            <span v-if="run.artifacts.length">{{ run.artifacts.length }} {{ run.artifacts.length === 1 ? "файл" : "файла" }}</span>
            <span v-for="artifact in run.artifacts.slice(0, 2)" :key="artifactName(artifact)">{{ artifactName(artifact) }}</span>
            <span v-if="run.privacy_findings_count" class="privacy-count">Privacy: {{ run.privacy_findings_count }}</span>
            <code>{{ shortId(run.id) }}</code>
          </span>
        </button>
      </section>

      <aside v-if="selectedId" class="run-detail" aria-label="Детали запуска">
        <button class="detail-panel__close" type="button" @click="closeSelection">Закрыть</button>
        <PageState :loading="selected.isPending.value" :error="selected.error.value" @retry="selected.refetch()">
          <template v-if="selected.data.value">
            <header class="run-detail__header">
              <span class="eyebrow">{{ shortId(selected.data.value.id) }} · {{ selected.data.value.source }}</span>
              <h2>{{ selected.data.value.question }}</h2>
              <div class="run-detail__status">
                <StatusBadge :value="selected.data.value.status" />
                <span>{{ formatDate(selected.data.value.updated_at) }}</span>
              </div>
            </header>

            <section v-if="selected.data.value.answer_markdown" class="run-section">
              <div class="run-section__header"><h3>Ответ</h3><span>{{ selected.data.value.citations.length }} источников</span></div>
              <pre class="markdown-output">{{ selected.data.value.answer_markdown }}</pre>
            </section>

            <section v-if="selected.data.value.artifacts.length" class="run-section">
              <div class="run-section__header"><h3>Артефакты</h3><span>{{ selected.data.value.artifacts.length }}</span></div>
              <article v-for="artifact in selected.data.value.artifacts" :key="fullArtifactName(artifact)" class="artifact-card">
                <div><strong>{{ fullArtifactName(artifact) }}</strong><small>Markdown · {{ artifact.content.length.toLocaleString("ru-RU") }} символов</small></div>
                <button class="button button--secondary button--small" type="button" @click="downloadArtifact(artifact)">Скачать</button>
                <details><summary>Посмотреть содержимое</summary><pre>{{ artifact.content }}</pre></details>
              </article>
            </section>

            <section v-if="selected.data.value.privacy_findings.length" class="run-section run-section--privacy">
              <div class="run-section__header"><h3>Privacy findings</h3><span>{{ selected.data.value.privacy_findings.length }}</span></div>
              <ul class="finding-list">
                <li v-for="finding in selected.data.value.privacy_findings" :key="`${finding.kind}:${finding.location}`"><strong>{{ finding.kind }}</strong><code>{{ finding.location }}</code></li>
              </ul>
            </section>

            <section v-if="selected.data.value.error_code" class="run-section run-section--error">
              <h3>Запуск остановлен</h3>
              <code>{{ selected.data.value.error_code }}</code>
            </section>

            <section v-if="selected.data.value.citations.length" class="run-section">
              <h3>Проверенные источники</h3>
              <ul class="citation-list">
                <li v-for="citation in selected.data.value.citations" :key="`${citation.path}:${citation.start_line}`"><code>{{ citation.path }}:{{ citation.start_line }}–{{ citation.end_line }}</code></li>
              </ul>
            </section>

            <section v-if="selected.data.value.uncertainty.length" class="run-section">
              <h3>Неопределённость</h3>
              <ul><li v-for="item in selected.data.value.uncertainty" :key="item">{{ item }}</li></ul>
            </section>

            <dl class="detail-list detail-list--compact">
              <dt>Проект</dt><dd>{{ projectName(projects.data.value, selected.data.value.project_id) }}</dd>
              <dt>Commit</dt><dd><code>{{ selected.data.value.commit_sha ?? "Нет" }}</code></dd>
              <dt>Correlation</dt><dd><code>{{ selected.data.value.correlation_id }}</code></dd>
              <dt>Provider</dt><dd><pre>{{ JSON.stringify(selected.data.value.provider_metadata, null, 2) }}</pre></dd>
            </dl>
          </template>
        </PageState>
      </aside>
    </div>
  </PageState>
</template>
