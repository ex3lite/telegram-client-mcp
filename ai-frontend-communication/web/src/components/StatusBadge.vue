<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ value: string }>();

const labels: Record<string, string> = {
  open: "Открыта",
  in_progress: "В работе",
  done: "Готово",
  rejected: "Отклонена",
  pending: "Ожидает ответа",
  answered: "Отвечено",
  expired: "Истекла",
  cancelled: "Отменена",
  ready: "Готов",
  syncing: "Синхронизация",
  stale: "Устарел",
  failed: "Ошибка",
  disabled: "Отключён",
  never_synced: "Не синхронизирован",
  success: "Успех",
  uncertain: "Неопределённо",
  active: "Активен",
  connected: "Подключён",
  stored: "Сохранён, не проверен",
  not_configured: "Не настроен",
  queued: "В очереди",
  generating: "Claude отвечает",
  answer_ready: "Ответ готов",
  published: "Опубликован"
};

const className = computed(() => `status status--${props.value}`);
</script>

<template>
  <span :class="className">{{ labels[value] ?? value }}</span>
</template>
