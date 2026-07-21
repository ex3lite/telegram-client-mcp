<script setup lang="ts">
import { ref } from "vue";
import { useQueryClient } from "@tanstack/vue-query";
import { useRoute, useRouter } from "vue-router";

import { api, ApiError } from "../api";
import type { AdminIdentity } from "../types";

const accessKey = ref("");
const submitting = ref(false);
const error = ref("");
const router = useRouter();
const route = useRoute();
const queryClient = useQueryClient();

async function submit() {
  submitting.value = true;
  error.value = "";
  try {
    const identity = await api<AdminIdentity>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ access_key: accessKey.value })
    });
    queryClient.setQueryData(["auth", "me"], identity);
    const target = typeof route.query.next === "string" ? route.query.next : "/overview";
    await router.replace(target);
  } catch (caught) {
    error.value =
      caught instanceof ApiError && (caught.status === 401 || caught.status === 422)
        ? "Неверный UUID-ключ доступа"
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
        <span class="product-mark__glyph" aria-hidden="true">KA</span>
        <span>Kakadu Agency</span>
      </div>
      <h1 id="login-title">Вход в операционную панель</h1>
      <p>Введите персональный UUID-ключ администратора.</p>
      <form @submit.prevent="submit">
        <label class="field">
          <span>UUID-ключ доступа</span>
          <input
            v-model="accessKey"
            autocomplete="current-password"
            inputmode="text"
            pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            spellcheck="false"
            type="password"
            required
          />
        </label>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button class="button button--primary" type="submit" :disabled="submitting">
          {{ submitting ? "Проверка..." : "Войти" }}
        </button>
      </form>
    </section>
  </main>
</template>
