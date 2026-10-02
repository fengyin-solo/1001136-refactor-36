<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">市政道路桥梁养护管理平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。</span>
        <span class="head-right">
          <RouterLink to="/bearing" class="notice-entry" title="支座维护通知：与桥梁档案、工程待办读同一批次结果">
            <span class="notice-bell">🔔</span>
            支座通知
            <em v-if="unreadCount" class="notice-badge">{{ unreadCount }}</em>
          </RouterLink>
          <span class="head-user">当前值班：{{ store.operator }} · {{ store.shiftLabel }}</span>
        </span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

import { fetchJson } from '@/api/client'
import { useSessionStore } from '@/stores/session'

const store = useSessionStore()

const navItems = [{ label: "运营概览", path: "/" }, { label: "路段管理", path: "/road_section" }, { label: "日常巡查", path: "/patrol" }, { label: "路面病害", path: "/pavement" }, { label: "桥梁定检", path: "/bridge" }, { label: "桥梁档案", path: "/bridge_info" }, { label: "隧道管养", path: "/tunnel" }, { label: "交安设施", path: "/traffic_facility" }, { label: "排水设施", path: "/drainage" }, { label: "绿化管养", path: "/green" }, { label: "路灯照明", path: "/lighting" }, { label: "除雪防滑", path: "/winter" }, { label: "防汛应急", path: "/flood" }, { label: "边坡防护", path: "/slope" }, { label: "伸缩缝管理", path: "/expansion" }, { label: "支座维护", path: "/bearing" }, { label: "养护工程", path: "/project" }, { label: "养护车辆", path: "/vehicle" }, { label: "养护材料", path: "/material" }]

// 通知入口：未读数来自已提交的支座谱系批次，和桥梁档案、工程待办同源。
const unreadCount = ref(0)
let timer: number | undefined

async function refreshNotifications() {
  try {
    const payload = await fetchJson<{ total: number }>('/api/bearing/notifications?only_unread=true')
    unreadCount.value = payload.total
  } catch {
    // 顶部铃铛是辅助入口，后端不可达时不打断页面
  }
}

onMounted(() => {
  void refreshNotifications()
  timer = window.setInterval(refreshNotifications, 15000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped>
.head-right { display: flex; align-items: center; gap: 16px; }
.notice-entry { position: relative; display: inline-flex; align-items: center; gap: 4px; color: var(--brand); text-decoration: none; font-size: 13px; }
.notice-bell { font-size: 15px; }
.notice-badge { font-style: normal; background: #d92d20; color: #fff; border-radius: 10px; padding: 0 6px; font-size: 12px; line-height: 16px; }
</style>
