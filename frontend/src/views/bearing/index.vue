<template>
  <section class="page" data-module="bearing">
    <header class="page-head">
      <div>
        <h2>支座维护管理</h2>
        <p class="page-desc">存量支座按支座编号迁移回填谱系；补检把检查证据、维护结论与建议写进同一批次，事务失败整体回滚。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" :disabled="migrating" @click="runMigration">
          {{ migrating ? '迁移中…' : '存量迁移回填' }}
        </button>
        <button class="btn primary" type="button" @click="openInspection">提交补检批次</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="showInspection" class="batch-form" @submit.prevent="submitInspection(false)">
      <div class="form-row">
        <label class="filter-item">
          <span>批次号（留空自动生成；相同批次号重复提交幂等）</span>
          <input v-model="inspectionForm.批次号" placeholder="例如 INSP-20261002-0001" />
        </label>
        <label class="filter-item">
          <span>所属桥梁（须已在桥梁档案建档）</span>
          <input v-model="inspectionForm.所属桥梁" placeholder="例如 桥梁档案样例1" />
        </label>
      </div>
      <div v-for="(item, index) in inspectionForm.检查明细" :key="index" class="form-row">
        <label class="filter-item">
          <span>支座编号</span>
          <input v-model="item.支座编号" placeholder="BEAR-0001" />
        </label>
        <label class="filter-item">
          <span>检查结论</span>
          <select v-model="item.检查结论">
            <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
          </select>
        </label>
        <label class="filter-item filter-wide">
          <span>检查证据</span>
          <input v-model="item.检查证据" placeholder="现场检查证据描述" />
        </label>
        <button class="btn ghost" type="button" @click="removeCheck(index)">删除</button>
      </div>
      <div class="form-actions">
        <button class="btn" type="button" @click="addCheck">增加一个支座</button>
        <button class="btn primary" type="button" @click="submitInspection(true)">提交并模拟事务失败</button>
        <button class="btn primary" type="submit">提交补检批次</button>
        <button class="btn ghost" type="button" @click="showInspection = false">取消</button>
      </div>
    </form>

    <form class="filter-bar" @submit.prevent="reloadBearings">
      <label class="filter-item">
        <span>支座编号</span>
        <input v-model="keyword" placeholder="按支座编号检索" />
      </label>
      <label class="filter-item">
        <span>支座状态</span>
        <input v-model="statusFilter" placeholder="正常/锈蚀/偏位/需更换" />
      </label>
      <button class="btn" type="submit">查询</button>
    </form>

    <h3 class="section-title">支座台账（含迁移批次号）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in bearings" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
        </tr>
        <tr v-if="!bearings.length">
          <td :colspan="columns.length" class="empty-state">暂无支座数据</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">谱系批次（证据 / 结论 / 建议同批，档案、待办、通知读同一批次）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次号</th>
          <th>批次类型</th>
          <th>所属桥梁</th>
          <th>支座编号</th>
          <th>维护结论</th>
          <th>提交时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="batch in batches" :key="String(batch.批次号)">
          <td>{{ batch.批次号 }}</td>
          <td>{{ batch.批次类型 }}</td>
          <td>{{ batch.所属桥梁 }}</td>
          <td>{{ joinBearingNos(batch.支座编号) }}</td>
          <td>{{ batch.维护结论 }}</td>
          <td>{{ batch.提交时间 }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openBatch(String(batch.批次号 ?? ''))">查看同批次结果</button>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td colspan="7" class="empty-state">暂无谱系批次，可先执行存量迁移或提交补检</td>
        </tr>
      </tbody>
    </table>

    <div v-if="batchDetail" class="batch-detail">
      <header class="detail-head">
        <h3>批次 {{ batchDetail.批次号 }} 的同源结果</h3>
        <button class="btn ghost" type="button" @click="batchDetail = null">关闭</button>
      </header>

      <h4 class="section-title">检查证据 · 支座维护结论 · 维护建议</h4>
      <table class="data-table">
        <thead>
          <tr><th>支座编号</th><th>检查证据</th><th>维护结论</th><th>维护建议</th></tr>
        </thead>
        <tbody>
          <tr v-for="bearingNo in batchDetail.支座编号" :key="bearingNo">
            <td>{{ bearingNo }}</td>
            <td>{{ eventContent('检查证据', bearingNo) }}</td>
            <td>{{ eventContent('支座维护结论', bearingNo) }}</td>
            <td>{{ eventContent('维护建议', bearingNo) }}</td>
          </tr>
        </tbody>
      </table>

      <h4 class="section-title">所属桥梁档案（同批次投影）</h4>
      <table class="data-table">
        <thead>
          <tr><th>桥梁编号</th><th>桥梁名称</th><th>桥梁状态</th><th>最近支座批次</th><th>最近支座检查</th></tr>
        </thead>
        <tbody>
          <tr v-for="archive in batchDetail.桥梁档案" :key="String(archive.id)">
            <td>{{ archive.桥梁编号 }}</td>
            <td>{{ archive.桥梁名称 }}</td>
            <td>{{ archive.桥梁状态 }}</td>
            <td>{{ archive.最近支座批次 }}</td>
            <td>{{ archive.最近支座检查 }}</td>
          </tr>
          <tr v-if="!batchDetail.桥梁档案.length">
            <td colspan="5" class="empty-state">该批次没有桥梁档案投影</td>
          </tr>
        </tbody>
      </table>

      <h4 class="section-title">工程待办（同批次派生）</h4>
      <table class="data-table">
        <thead>
          <tr><th>待办编号</th><th>支座编号</th><th>处置动作</th><th>优先级</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="todo in batchDetail.工程待办" :key="String(todo.id)">
            <td>{{ todo.待办编号 }}</td>
            <td>{{ todo.支座编号 }}</td>
            <td>{{ todo.处置动作 }}</td>
            <td>{{ todo.优先级 }}</td>
            <td>{{ todo.状态 }}</td>
          </tr>
          <tr v-if="!batchDetail.工程待办.length">
            <td colspan="5" class="empty-state">结论均正常，无待办</td>
          </tr>
        </tbody>
      </table>

      <h4 class="section-title">通知入口（同批次生成）</h4>
      <table class="data-table">
        <thead>
          <tr><th>级别</th><th>标题</th><th>内容</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="notice in batchDetail.通知" :key="String(notice.id)">
            <td>{{ notice.级别 }}</td>
            <td>{{ notice.标题 }}</td>
            <td>{{ notice.内容 }}</td>
            <td>{{ notice.创建时间 }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span>共 {{ bearingTotal }} 条支座记录 · {{ batches.length }} 个谱系批次</span>
      <span v-if="message" :class="messageOk ? '' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | string[] | null>
type BatchDetail = Row & {
  检查证据: Row[]
  支座维护结论: Row[]
  维护建议: Row[]
  桥梁档案: Row[]
  工程待办: Row[]
  通知: Row[]
  支座编号: string[]
}

const ENDPOINT = '/api/bearing'
const columns = ['支座编号', '所属桥梁', '支座类型', '设计承载力', '位移量', '锈蚀程度', '最近检查', '支座状态', '谱系批次号']
const statuses = ['正常', '锈蚀', '偏位', '需更换']
const stats = ref([
  { label: '已迁移支座', value: 0 },
  { label: '谱系批次', value: 0 },
  { label: '待处理工程待办', value: 0 },
])

const bearings = ref<Row[]>([])
const bearingTotal = ref(0)
const batches = ref<Row[]>([])
const batchDetail = ref<BatchDetail | null>(null)
const keyword = ref('')
const statusFilter = ref('')
const message = ref('')
const messageOk = ref(true)
const migrating = ref(false)
const showInspection = ref(false)

function emptyCheck() {
  return { 支座编号: '', 检查证据: '', 检查结论: '正常', 维护建议: '', 检查人: '' }
}

const inspectionForm = reactive<{
  批次号: string
  所属桥梁: string
  检查明细: ReturnType<typeof emptyCheck>[]
  模拟失败: boolean
}>({
  批次号: '',
  所属桥梁: '桥梁档案样例1',
  检查明细: [emptyCheck()],
  模拟失败: false,
})

function notify(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function openInspection() {
  showInspection.value = true
}

function addCheck() {
  inspectionForm.检查明细.push(emptyCheck())
}

function removeCheck(index: number) {
  inspectionForm.检查明细.splice(index, 1)
}

async function runMigration() {
  migrating.value = true
  try {
    const response = await request(`${ENDPOINT}/migration`, { method: 'POST' })
    const payload = await response.json()
    notify(payload.message ?? '存量迁移已完成', response.ok)
    await reloadAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '存量迁移失败', false)
  } finally {
    migrating.value = false
  }
}

async function submitInspection(simulateFailure = false) {
  inspectionForm.模拟失败 = simulateFailure
  const response = await request(`${ENDPOINT}/inspections`, {
    method: 'POST',
    body: JSON.stringify(inspectionForm),
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    notify(payload?.detail ?? '补检批次未落地，已整体回滚', false)
    return
  }
  notify(payload.message ?? '补检批次已提交', true)
  showInspection.value = false
  await reloadAll()
  if (payload.entry?.批次号) {
    await openBatch(String(payload.entry.批次号))
  }
}

async function reloadBearings() {
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  const response = await request(`${ENDPOINT}?${query.toString()}`)
  if (!response.ok) {
    notify('桥梁支座列表读取失败', false)
    return
  }
  const payload = await response.json()
  bearings.value = payload.items ?? []
  bearingTotal.value = payload.total ?? bearings.value.length
}

async function reloadBatches() {
  const response = await request(`${ENDPOINT}/batches?size=200`)
  if (!response.ok) return
  const payload = await response.json()
  batches.value = payload.items ?? []
}

async function reloadStats() {
  const [bearingsResp, todosResp] = await Promise.all([
    request(`${ENDPOINT}?size=1`),
    request(`${ENDPOINT}/todos`),
  ])
  const bearingPayload = await bearingsResp.json()
  const todoPayload = await todosResp.json()
  stats.value[0].value = bearingPayload.total ?? 0
  stats.value[1].value = batches.value.length
  stats.value[2].value = todoPayload.total ?? 0
}

async function openBatch(batchNo: string) {
  const response = await request(`${ENDPOINT}/batches/${encodeURIComponent(batchNo)}`)
  if (!response.ok) {
    notify('批次详情读取失败', false)
    return
  }
  batchDetail.value = (await response.json()) as BatchDetail
}

function joinBearingNos(value: Row[string]): string {
  return Array.isArray(value) ? value.join('、') : String(value ?? '')
}

function eventContent(kind: '检查证据' | '支座维护结论' | '维护建议', bearingNo: string): string {
  const group = batchDetail.value?.[kind] ?? []
  return group
    .filter((event) => event.支座编号 === bearingNo)
    .map((event) => String(event.内容 ?? ''))
    .join('；')
}

async function reloadAll() {
  await reloadBearings()
  await reloadBatches()
  await reloadStats()
}

onMounted(reloadAll)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; }
.section-title { font-size: 14px; margin: 18px 0 8px; }
.batch-form { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.form-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 8px; }
.filter-wide { flex: 1; min-width: 260px; }
.form-actions { display: flex; gap: 8px; justify-content: flex-end; }
.batch-detail { margin-top: 16px; border-top: 2px solid var(--border); padding-top: 8px; }
.detail-head { display: flex; justify-content: space-between; align-items: center; }
</style>
