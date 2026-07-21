<script setup lang="ts">
defineProps<{
  loading: boolean;
  error?: Error | null;
  empty?: boolean;
  emptyTitle?: string;
}>();

defineEmits<{ retry: [] }>();
</script>

<template>
  <div v-if="loading" class="page-state" aria-live="polite" aria-busy="true">
    <div class="skeleton-line skeleton-line--wide"></div>
    <div class="skeleton-line"></div>
    <span>Загрузка данных</span>
  </div>
  <div v-else-if="error" class="page-state page-state--error" role="alert">
    <strong>Данные не загрузились</strong>
    <span>{{ error.message }}</span>
    <button class="cds--btn cds--btn--sm cds--btn--tertiary" type="button" @click="$emit('retry')">
      Повторить
    </button>
  </div>
  <div v-else-if="empty" class="page-state">
    <strong>{{ emptyTitle ?? "Здесь пока ничего нет" }}</strong>
    <span>Новые записи появятся автоматически после первого рабочего сценария.</span>
  </div>
  <slot v-else></slot>
</template>

