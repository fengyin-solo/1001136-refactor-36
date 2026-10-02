<template>
  <section class="page" data-module="bearing">
    <header class="page-head">
      <div>
        <h2>支座维护管理</h2>
        <p class="page-desc">检查证据、维护结论与处置建议写入同一谱系批次；所属桥梁档案、工程待办和通知入口读取同一批次结果。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出支座维护清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 并发补检：按批次号幂等，重复提交不追加事件 -->
    <form class="filter-bar inspection-panel" @submit.prevent="submitInspection">
      <label class="filter-item">
        <span>批次号</span>
        <input v-model="form.批次号" placeholder="如 CHK-20261002-001，重复提交自动幂等" />
      </label>
      <label class="filter-item">
        <span>支座编号</span>
        <input v-model="form.支座编号" list="bearing-codes" placeholder="按支座编号定位" />
        <datalist id="bearing-codes">
          <option v-for="row in rows" :key="String(row.id)" :value="String(row['支座编号'])" />
        </datalist>
      </label>
      <label class="filter-item">
        <span>检查人</span>
        <input v-model="form.检查人" placeholder="补检检查人" />
      </label>
      <label class="filter-item filter-item-wide">
        <span>检查证据</span>
        <input v-model="form.检查证据" placeholder="照片编号 / 现场记录，证据与结论同批落地" />
      </label>
      <label class="filter-item">
        <span>维护结论</span>
        <select v-model="form.维护结论">
          <option value="" disabled>选择结论</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="filter-item filter-item-wide">
        <span>处置建议</span>
        <input v-model="form.处置建议" placeholder="建议与结论必须写在同一批次" />
      </label>
      <button class="btn primary" type="submit">提交补检批次</button>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>批次号</span>
        <input v-model="filters.batch_no" placeholder="按批次号过滤" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>当前批次号</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['当前批次号'] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无支座维护数据</td>
        </tr>
      </tbody>
    </table>

    <!-- 三个入口读取同一批次结果 -->
    <div class="lineage-grid">
      <article class="lineage-card">
        <h3>所属桥梁档案 · 谱系批次</h3>
        <ul class="lineage-list">
          <li v-for="item in archiveItems" :key="String(item['所属桥梁'])">
            <strong>{{ item['所属桥梁'] }}</strong>
            <span>最近批次：{{ item['最近批次号'] }}</span>
            <span v-if="item['本批结论'] && Object.keys(item['本批结论']).length">
              本批结论：{{ formatMap(item['本批结论']) }}
            </span>
          </li>
          <li v-if="!archiveItems.length" class="empty-state">暂无桥梁谱系</li>
        </ul>
      </article>

      <article class="lineage-card">
        <h3>工程待办 · 批次派生</h3>
        <ul class="lineage-list">
          <li v-for="todo in todoItems" :key="String(todo.id)">
            <strong>{{ todo['支座编号'] }} · {{ todo['待办动作'] }}</strong>
            <span>{{ todo['所属桥梁'] }}（{{ todo['批次号'] }}）</span>
            <span>{{ todo['处置建议'] }}</span>
          </li>
          <li v-if="!todoItems.length" class="empty-state">暂无工程待办</li>
        </ul>
      </article>

      <article class="lineage-card">
        <h3>通知入口 · 批次结论</h3>
        <ul class="lineage-list">
          <li v-for="notice in notificationItems" :key="String(notice.id)">
            <strong>[{{ notice['级别'] }}] {{ notice['批次号'] }}</strong>
            <span>{{ notice['内容'] }}</span>
          </li>
          <li v-if="!notificationItems.length" class="empty-state">暂无批次通知</li>
        </ul>
      </article>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条支座维护记录</span>
      <span v-if="successMessage" class="success-text">{{ successMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null | Record<string, string>>

const ENDPOINT = '/api/bearing'
const columns = ["支座编号", "所属桥梁", "支座类型", "设计承载力", "位移量", "锈蚀程度", "最近检查", "支座状态"]
const actions = ["防锈处理", "纠偏复位", "安排更换"]
const statuses = ["正常", "锈蚀", "偏位", "需更换"]
const stats = ref([{"label": "正常支座", "value": 0}, {"label": "锈蚀支座", "value": 0}, {"label": "需更换支座", "value": 0}])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 2)

const archiveItems = ref<Row[]>([])
const todoItems = ref<Row[]>([])
const notificationItems = ref<Row[]>([])

const form = reactive({
  批次号: `CHK-${new Date().toISOString().slice(0, 10).replace(/-/g, '')}-001`,
  支座编号: '',
  检查人: '',
  检查证据: '',
  维护结论: '',
  处置建议: '',
})

function formatMap(map: unknown): string {
  if (!map || typeof map !== 'object') return '—'
  return Object.entries(map as Record<string, string>).map(([key, value]) => `${key}=${value}`).join('，')
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function submitInspection() {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/inspections`, {
      method: 'POST',
      body: JSON.stringify({ values: { ...form } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '补检批次未落地，已整体回滚')
    }
    successMessage.value = payload.replayed
      ? `批次 ${payload.batch_no} 已落地，按批次号幂等回读，未重复追加事件`
      : `批次 ${payload.batch_no} 已落地：证据、结论、建议同批提交`
    await Promise.all([reload(), reloadLineage()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补检批次提交失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '支座维护动作未生效，请稍后重试')
    }
    successMessage.value = '维护动作已随谱系批次落地'
    await Promise.all([reload(), reloadLineage()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '支座维护操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters.value)) {
    if (value) params.set(key, value)
  }
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('桥梁支座列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = rows.value.filter((r) => r['status'] === '正常').length
    stats.value[1].value = rows.value.filter((r) => r['status'] === '锈蚀').length
    stats.value[2].value = rows.value.filter((r) => r['status'] === '需更换').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '支座维护列表读取失败'
  }
}

async function reloadLineage() {
  try {
    const [archive, todos, notifications] = await Promise.all([
      request(`${ENDPOINT}/lineage`),
      request(`${ENDPOINT}/todos`),
      request(`${ENDPOINT}/notifications?unread=true`),
    ])
    archiveItems.value = (await archive.json()).items ?? []
    todoItems.value = (await todos.json()).items ?? []
    notificationItems.value = (await notifications.json()).items ?? []
  } catch {
    // 三个入口读失败时保留旧数据，只在列表错误处提示
  }
}

onMounted(() => {
  void reload()
  void reloadLineage()
})
</script>

<style scoped>
.inspection-panel {
  align-items: flex-end;
  flex-wrap: wrap;
}

.filter-item-wide {
  flex: 2 1 220px;
}

.success-text {
  color: #1a7f37;
}

.lineage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
  margin-top: 16px;
}

.lineage-card {
  border: 1px solid #e3e6ea;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fff;
}

.lineage-card h3 {
  margin: 0 0 8px;
  font-size: 14px;
}

.lineage-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.lineage-list li {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: #4b5563;
  padding-bottom: 8px;
  border-bottom: 1px dashed #eef0f3;
}

.lineage-list li strong {
  font-size: 13px;
  color: #1f2937;
}
</style>
