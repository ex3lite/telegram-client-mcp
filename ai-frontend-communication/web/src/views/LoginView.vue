<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { api, ApiError } from "../api";

const email = ref("");
const password = ref("");
const submitting = ref(false);
const error = ref("");
const router = useRouter();
const route = useRoute();

async function submit() {
  submitting.value = true;
  error.value = "";
  try {
    await api("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email: email.value, password: password.value })
    });
    const target = typeof route.query.next === "string" ? route.query.next : "/overview";
    await router.replace(target);
  } catch (caught) {
    error.value =
      caught instanceof ApiError && caught.status === 401
        ? "Неверный email или пароль"
        : "Сервис авторизации временно недоступен";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-panel" aria-labelledby="login-title">
      <div class="product-mark product-mark--login">
        <span class="product-mark__glyph" aria-hidden="true">DC</span>
        <span>Developer Agent</span>
      </div>
      <h1 id="login-title">Вход в операционную панель</h1>
      <p>Локальная учётная запись владельца системы.</p>
      <form @submit.prevent="submit">
        <label class="field">
          <span>Email</span>
          <input v-model="email" autocomplete="username" type="email" required />
        </label>
        <label class="field">
          <span>Пароль</span>
          <input v-model="password" autocomplete="current-password" type="password" required />
        </label>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button class="cds--btn cds--btn--primary" type="submit" :disabled="submitting">
          {{ submitting ? "Проверка..." : "Войти" }}
        </button>
      </form>
    </section>
  </main>
</template>

