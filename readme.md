# 🖥️ Custom RMM: Enterprise Fleet Management (R&D)

> **System klasy Remote Monitoring & Management (RMM) do centralnego zarządzania rozproszoną infrastrukturą. Projekt zorientowany na bezpieczeństwo operacyjne, skalowalność i automatyzację.**

---

## 📑 Wizja Projektu (R&D Context)

Projekt **RMM** powstał jako wyzwanie inżynierskie: **Jak zbudować bezpieczny, lekki i darmowy system do zarządzania tysiącami końcówek (Linux/Windows) bez polegania na kosztownych licencjach SaaS?**

Głównym celem badawczym było stworzenie narzędzia typu "Full-Stack Security", które zapewnia pełną widoczność (Visibility) floty i możliwość natychmiastowej reakcji (Actionability). Projekt został zrealizowany przy użyciu **AI jako akceleratora**, co pozwoliło na wdrożenie zaawansowanych funkcji (MFA, WebSockety, Terminal) w rekordowo krótkim czasie.

---

## 🚀 Kluczowe Funkcjonalności

### 1. Real-Time Monitoring (Socket.IO)
* **Live Metrics**: Strumieniowanie metryk CPU, RAM, dysku i sieci w czasie rzeczywistym.
* **Interactive Dashboards**: Wizualizacja kondycji agentów przy użyciu Chart.js.

### 2. Zaawansowane Bezpieczeństwo (Security-First)
* **MFA (TOTP)**: Pełna implementacja uwierzytelniania dwuskładnikowego (Google Authenticator) z kodami QR i szyfrowanymi kodami zapasowymi.
* **RBAC (Role-Based Access Control)**: System uprawnień (Admin, Command Sender, Viewer) kontrolujący dostęp do krytycznych operacji.
* **Audit Logging**: Szczegółowy dziennik zdarzeń rejestrujący każdą akcję administracyjną i polecenia shell.

### 3. Operacje Zdalne (Incident Response)
* **Interactive Terminal (Xterm.js)**: Dostęp do interaktywnej powłoki (PTY) bezpośrednio z przeglądarki.
* **Custom Command Library**: Centralna baza skryptów do automatyzacji zadań (np. setup usług, listowanie procesów).
* **Agent Self-Update**: Autorski mechanizm zdalnej aktualizacji kodu agenta bez utraty połączenia.

---

## 🏗️ Architektura Systemu



* **Serwer**: Flask (Python) + Flask-SocketIO + SQLite.
* **Agent**: Lekki, asynchroniczny skrypt Python kompatybilny z Linux i Windows (wrapper PowerShell).
* **Bezpieczeństwo**: Flask-Talisman (Content Security Policy), Fernet (szyfrowanie sekretów MFA), API Keys.

