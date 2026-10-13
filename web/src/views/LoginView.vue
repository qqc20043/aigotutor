<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { errorText } from '@/api/http'

const router = useRouter()
const route = useRoute()
const user = useUserStore()

const mode = ref<'login' | 'register'>('login')
const formRef = ref<FormInstance>()

const form = reactive({
  username: 'demo',
  password: 'demo123456',
  nickname: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 32, message: '长度 3-32 个字符', trigger: 'blur' },
    // 与后端 RegisterIn 的 pattern 保持一致：仅字母数字与 _-.
    {
      pattern: /^[A-Za-z0-9_\-.]+$/,
      message: '仅支持字母、数字、下划线、连字符、点',
      trigger: 'blur',
    },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    // 后端 password_strength 要求至少 8 位，且不能是纯数字/纯字母
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
}

async function submit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  try {
    if (mode.value === 'login') {
      await user.login(form.username, form.password)
      ElMessage.success('登录成功')
    } else {
      await user.register(form.username, form.password, form.nickname || undefined)
      ElMessage.success('注册成功，已自动登录')
    }
    const redirect = (route.query.redirect as string) || '/dashboard'
    void router.push(redirect)
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

function fillDemo() {
  mode.value = 'login'
  form.username = 'demo'
  form.password = 'demo123456'
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <div class="brand-mark">AIgoTutor</div>
        <div class="brand-sub">多智能体算法刷题平台</div>
      </div>

      <el-radio-group v-model="mode" style="width: 100%; margin-bottom: 18px">
        <el-radio-button value="login" style="flex: 1">登录</el-radio-button>
        <el-radio-button value="register" style="flex: 1">注册</el-radio-button>
      </el-radio-group>

      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" size="large">
            <template #prefix><el-icon><User /></el-icon></template>
          </el-input>
        </el-form-item>

        <el-form-item v-if="mode === 'register'" label="昵称" prop="nickname">
          <el-input v-model="form.nickname" placeholder="可选" size="large">
            <template #prefix><el-icon><Postcard /></el-icon></template>
          </el-input>
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            size="large"
            show-password
            @keyup.enter="submit"
          >
            <template #prefix><el-icon><Lock /></el-icon></template>
          </el-input>
        </el-form-item>

        <el-button
          type="primary"
          size="large"
          style="width: 100%"
          :loading="user.loading"
          @click="submit"
        >
          {{ mode === 'login' ? '登录' : '注册并登录' }}
        </el-button>
      </el-form>

      <div class="hint">
        <el-text size="small" type="info">演示账号 demo / demo123456</el-text>
        <el-button link type="primary" size="small" @click="fillDemo">填入</el-button>
      </div>

      <div class="capability">
        <el-tag size="small" effect="plain">判题沙箱</el-tag>
        <el-tag size="small" effect="plain">Agent 流式</el-tag>
        <el-tag size="small" effect="plain">FSRS 复习</el-tag>
        <el-tag size="small" effect="plain">掌握度画像</el-tag>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2d3d 0%, #2c3e50 45%, #409eff 140%);
}

.login-card {
  width: 400px;
  padding: 34px 32px 26px;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28);
}

.brand {
  text-align: center;
  margin-bottom: 22px;
}

.brand-mark {
  font-size: 26px;
  font-weight: 700;
  color: #1f2d3d;
  letter-spacing: 0.5px;
}

.brand-sub {
  color: #909399;
  font-size: 13px;
  margin-top: 6px;
}

:deep(.el-radio-button) {
  flex: 1;
}

:deep(.el-radio-button__inner) {
  width: 100%;
}

.hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: 14px;
}

.capability {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: center;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px dashed #ebeef5;
}
</style>
