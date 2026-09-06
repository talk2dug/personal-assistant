import { createContext, useContext, useEffect, useState } from 'react'
import { api, ApiError } from '../api'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(undefined) // undefined = still checking, null = logged out
  const [error, setError] = useState(null)

  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null))
  }, [])

  async function login(name, password) {
    setError(null)
    try {
      const result = await api.login(name, password)
      setUser(result)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Login failed')
      throw err
    }
  }

  async function logout() {
    await api.logout()
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, error, login, logout }}>{children}</AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
