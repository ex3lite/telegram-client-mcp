<script setup lang="ts">
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
  uncertain: "Неопределённо"
};

const className = `status status--${props.value}`;
</script>

<template>
  <span :class="className">{{ labels[value] ?? value }}</span>
</template>

