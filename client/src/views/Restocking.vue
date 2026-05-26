<template>
  <div class="restocking">
    <!-- Success confirmation banner — shown after a successful order placement.
         Auto-clears when the user moves the budget slider (see watch below). -->
    <div v-if="lastSubmittedOrder" class="success-banner">
      {{ t('restocking.orderPlaced', {
        orderNumber: lastSubmittedOrder.order_number,
        date: formatDate(lastSubmittedOrder.expected_delivery),
        days: lastSubmittedOrder.lead_time_days
      }) }}
    </div>

    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <!-- Budget panel -->
    <div class="card budget-card">
      <div class="budget-layout">
        <div class="budget-label-group">
          <span class="budget-label">{{ t('restocking.budgetLabel') }}</span>
          <span class="budget-value">{{ currencySymbol }}{{ budget.toLocaleString() }}</span>
        </div>
        <input
          type="range"
          class="budget-slider"
          :min="1000"
          :max="500000"
          :step="1000"
          v-model.number="budget"
        />
        <div class="budget-range-labels">
          <span>{{ currencySymbol }}1,000</span>
          <span>{{ currencySymbol }}500,000</span>
        </div>
      </div>
    </div>

    <!-- Summary stats -->
    <div class="stats-grid">
      <div class="stat-card info">
        <div class="stat-label">{{ t('restocking.recommendedItems') }}</div>
        <div class="stat-value">{{ recommendations.length }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">{{ t('restocking.totalCost') }}</div>
        <div class="stat-value">{{ currencySymbol }}{{ totalCost.toLocaleString() }}</div>
      </div>
      <div class="stat-card" :class="remainingBudget >= 0 ? 'success' : 'danger'">
        <div class="stat-label">{{ t('restocking.remainingBudget') }}</div>
        <div class="stat-value">{{ currencySymbol }}{{ remainingBudget.toLocaleString() }}</div>
      </div>
    </div>

    <!-- Recommendations table card -->
    <div class="card">
      <div class="card-header">
        <h3 class="card-title">{{ t('restocking.recommendations') }}</h3>
      </div>

      <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
      <div v-else-if="error" class="error">{{ error }}</div>
      <div v-else-if="recommendations.length === 0" class="empty-state">
        {{ t('restocking.noRecommendations') }}
      </div>
      <div v-else class="table-container">
        <table class="recommendations-table">
          <thead>
            <tr>
              <th>{{ t('restocking.table.sku') }}</th>
              <th>{{ t('restocking.table.itemName') }}</th>
              <th>{{ t('restocking.table.warehouse') }}</th>
              <th class="col-num">{{ t('restocking.table.quantity') }}</th>
              <th class="col-num">{{ t('restocking.table.unitCost') }}</th>
              <th class="col-num">{{ t('restocking.table.totalCost') }}</th>
              <th>{{ t('restocking.table.leadTime') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in recommendations" :key="item.item_sku">
              <td><strong>{{ item.item_sku }}</strong></td>
              <td>{{ item.item_name }}</td>
              <td>{{ item.warehouse }}</td>
              <td class="col-num">{{ item.suggested_quantity }}</td>
              <td class="col-num">{{ currencySymbol }}{{ item.unit_cost.toLocaleString() }}</td>
              <td class="col-num"><strong>{{ currencySymbol }}{{ item.total_cost.toLocaleString() }}</strong></td>
              <td>{{ t('restocking.leadTimeDays', { days: item.lead_time_days }) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Place Order button -->
    <div class="action-row">
      <button
        class="btn-primary"
        :disabled="recommendations.length === 0 || placing"
        @click="placeOrder"
      >
        {{ placing ? t('restocking.placing') : t('restocking.placeOrder') }}
      </button>
    </div>
  </div>
</template>

<script>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, currentLocale } = useI18n()

    const currencySymbol = computed(() => currentCurrency.value === 'JPY' ? '¥' : '$')

    const budget = ref(50000)
    const recommendations = ref([])
    const loading = ref(false)
    const error = ref(null)
    const placing = ref(false)
    const lastSubmittedOrder = ref(null)

    // Derived totals
    const totalCost = computed(() =>
      recommendations.value.reduce((sum, item) => sum + item.total_cost, 0)
    )

    const remainingBudget = computed(() => budget.value - totalCost.value)

    const formatDate = (dateString) => {
      if (!dateString) return '-'
      const locale = currentLocale.value === 'ja' ? 'ja-JP' : 'en-US'
      const date = new Date(dateString)
      if (isNaN(date.getTime())) return dateString
      return date.toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' })
    }

    // Debounce timer handle — we keep one ref so the previous timeout can be
    // cancelled when the slider fires again before 250 ms have elapsed.
    let debounceTimer = null

    const loadRecommendations = async () => {
      loading.value = true
      error.value = null
      try {
        const data = await api.getRestockingRecommendations(budget.value)
        recommendations.value = data
      } catch (err) {
        error.value = 'Failed to load recommendations: ' + err.message
        console.error(err)
      } finally {
        loading.value = false
      }
    }

    // Watch budget changes with a ~250 ms debounce so we don't spam the API
    // while the user is dragging the slider. Also clear any confirmation banner
    // because it belongs to the previous order / budget state.
    watch(budget, () => {
      // Dismiss any previous order confirmation when the slider moves
      lastSubmittedOrder.value = null

      clearTimeout(debounceTimer)
      debounceTimer = setTimeout(() => {
        loadRecommendations()
      }, 250)
    })

    const placeOrder = async () => {
      if (recommendations.value.length === 0) return
      placing.value = true
      error.value = null
      try {
        // Map recommendation shape -> RestockingOrderItem shape the backend
        // expects: `suggested_quantity` becomes `quantity`, and extra forecast
        // fields (current_demand, forecasted_demand, lead_time_days) are dropped.
        const orderItems = recommendations.value.map(r => ({
          item_sku: r.item_sku,
          item_name: r.item_name,
          warehouse: r.warehouse,
          quantity: r.suggested_quantity,
          unit_cost: r.unit_cost,
          total_cost: r.total_cost
        }))
        const result = await api.placeRestockingOrder({
          items: orderItems,
          budget: budget.value
        })
        lastSubmittedOrder.value = result
        // Re-fetch so the list reflects the latest state from the backend
        await loadRecommendations()
      } catch (err) {
        error.value = 'Failed to place order: ' + err.message
        console.error(err)
      } finally {
        placing.value = false
      }
    }

    onMounted(() => loadRecommendations())

    return {
      t,
      currencySymbol,
      budget,
      recommendations,
      loading,
      error,
      placing,
      lastSubmittedOrder,
      totalCost,
      remainingBudget,
      formatDate,
      placeOrder
    }
  }
}
</script>

<style scoped>
/* Success banner — mirrors the structure of the global .error class but with
   the success palette (#d1fae5 bg, #065f46 text) from .badge.success */
.success-banner {
  background: #d1fae5;
  border: 1px solid #6ee7b7;
  color: #065f46;
  padding: 1rem;
  border-radius: 8px;
  margin-bottom: 1.25rem;
  font-size: 0.938rem;
  font-weight: 500;
}

/* Budget panel */
.budget-card {
  margin-bottom: 1.25rem;
}

.budget-layout {
  display: flex;
  flex-direction: column;
  gap: 0.625rem;
}

.budget-label-group {
  display: flex;
  align-items: baseline;
  gap: 1rem;
}

.budget-label {
  font-size: 0.875rem;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.budget-value {
  font-size: 2.25rem;
  font-weight: 700;
  color: #0f172a;
  letter-spacing: -0.025em;
}

.budget-slider {
  width: 100%;
  accent-color: #2563eb;
  cursor: pointer;
  height: 6px;
}

.budget-range-labels {
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  color: #94a3b8;
}

/* Recommendations table */
.recommendations-table {
  width: 100%;
  table-layout: auto;
}

.col-num {
  text-align: right;
}

/* Empty state */
.empty-state {
  padding: 3rem;
  text-align: center;
  color: #64748b;
  font-size: 0.938rem;
}

/* Place Order action row */
.action-row {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 2rem;
}

.btn-primary {
  padding: 0.75rem 2rem;
  background: #2563eb;
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 0.938rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s ease, transform 0.1s ease;
}

.btn-primary:hover:not(:disabled) {
  background: #1d4ed8;
  transform: translateY(-1px);
}

.btn-primary:disabled {
  background: #94a3b8;
  cursor: not-allowed;
  transform: none;
}
</style>
