/**
 * WebSocket client for real-time inbox updates.
 * Connects to /ws/inbox with JWT authentication.
 * Emits DOM events for Alpine.js integration.
 */
class InboxWebSocket {
    constructor(options = {}) {
        this.ws = null;
        this.token = options.token || null;
        this.connected = false;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 10;
        this.baseReconnectDelay = 1000;
        this.maxReconnectDelay = 30000;
        this.heartbeatInterval = null;
        this.heartbeatTimeout = 25000;
        this.subscriptions = new Set();
        this.onStatusChange = options.onStatusChange || (() => {});
    }

    /**
     * Connect to WebSocket server.
     * @param {string} token - JWT access token
     */
    connect(token) {
        if (token) {
            this.token = token;
        }

        if (!this.token) {
            console.warn('[InboxWS] No token provided, cannot connect');
            return;
        }

        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${window.location.host}/ws/inbox?token=${encodeURIComponent(this.token)}`;

        try {
            this.ws = new WebSocket(url);
            this._setupEventHandlers();
        } catch (error) {
            console.error('[InboxWS] Connection error:', error);
            this._scheduleReconnect();
        }
    }

    _setupEventHandlers() {
        this.ws.onopen = () => {
            console.log('[InboxWS] Connected');
            this.connected = true;
            this.reconnectAttempts = 0;
            this.onStatusChange(true);
            this._startHeartbeat();
            this._resubscribe();
            this._emitEvent('inbox-ws-connected', {});
        };

        this.ws.onclose = (event) => {
            console.log('[InboxWS] Disconnected', event.code, event.reason);
            this.connected = false;
            this.onStatusChange(false);
            this._stopHeartbeat();
            this._emitEvent('inbox-ws-disconnected', { code: event.code, reason: event.reason });

            // Don't reconnect on auth failure
            if (event.code === 4001) {
                console.warn('[InboxWS] Authentication failed, not reconnecting');
                return;
            }

            this._scheduleReconnect();
        };

        this.ws.onerror = (error) => {
            console.error('[InboxWS] Error:', error);
        };

        this.ws.onmessage = (event) => {
            this._handleMessage(event.data);
        };
    }

    _handleMessage(data) {
        try {
            const message = JSON.parse(data);
            const eventType = message.event;
            const eventData = message.data || {};

            switch (eventType) {
                case 'message_new':
                    this._emitEvent('inbox-new-message', eventData);
                    break;
                case 'message_status_changed':
                    this._emitEvent('inbox-message-status', eventData);
                    break;
                case 'conversation_updated':
                    this._emitEvent('inbox-conversation-updated', eventData);
                    break;
                case 'conversation_summary':
                    this._emitEvent('inbox-conversation-summary', eventData);
                    break;
                case 'user_typing':
                    this._emitEvent('inbox-user-typing', eventData);
                    break;
                case 'connection_ack':
                    console.log('[InboxWS] Connection acknowledged:', eventData.user_id);
                    break;
                case 'heartbeat':
                    // Heartbeat received, connection is healthy
                    break;
                default:
                    console.log('[InboxWS] Unknown event:', eventType, eventData);
            }
        } catch (error) {
            console.error('[InboxWS] Failed to parse message:', error);
        }
    }

    _emitEvent(name, detail) {
        window.dispatchEvent(new CustomEvent(name, { detail }));
    }

    _scheduleReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.warn('[InboxWS] Max reconnect attempts reached');
            return;
        }

        const delay = Math.min(
            this.baseReconnectDelay * Math.pow(2, this.reconnectAttempts),
            this.maxReconnectDelay
        );
        this.reconnectAttempts++;

        console.log(`[InboxWS] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);
        setTimeout(() => this.connect(), delay);
    }

    _startHeartbeat() {
        this._stopHeartbeat();
        this.heartbeatInterval = setInterval(() => {
            if (this.connected) {
                this.send({ type: 'ping' });
            }
        }, this.heartbeatTimeout);
    }

    _stopHeartbeat() {
        if (this.heartbeatInterval) {
            clearInterval(this.heartbeatInterval);
            this.heartbeatInterval = null;
        }
    }

    _resubscribe() {
        // Re-subscribe to all conversations after reconnect
        for (const conversationId of this.subscriptions) {
            this.send({ type: 'subscribe', conversation_id: conversationId });
        }
    }

    /**
     * Send a message to the server.
     * @param {object} message - Message object to send
     */
    send(message) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(message));
        }
    }

    /**
     * Subscribe to a conversation for real-time updates.
     * @param {string} conversationId - Conversation ID to subscribe to
     */
    subscribe(conversationId) {
        if (!conversationId) return;
        this.subscriptions.add(conversationId);
        this.send({ type: 'subscribe', conversation_id: conversationId });
    }

    /**
     * Unsubscribe from a conversation.
     * @param {string} conversationId - Conversation ID to unsubscribe from
     */
    unsubscribe(conversationId) {
        if (!conversationId) return;
        this.subscriptions.delete(conversationId);
        this.send({ type: 'unsubscribe', conversation_id: conversationId });
    }

    /**
     * Send typing indicator.
     * @param {string} conversationId - Conversation ID
     * @param {boolean} isTyping - Whether user is typing
     */
    sendTyping(conversationId, isTyping = true) {
        if (!conversationId) return;
        this.send({
            type: 'typing',
            conversation_id: conversationId,
            data: { is_typing: isTyping }
        });
    }

    /**
     * Disconnect from the WebSocket server.
     */
    disconnect() {
        this._stopHeartbeat();
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.connected = false;
        this.subscriptions.clear();
    }

    /**
     * Check if currently connected.
     * @returns {boolean}
     */
    isConnected() {
        return this.connected && this.ws && this.ws.readyState === WebSocket.OPEN;
    }
}

// Export for module systems and make available globally
if (typeof module !== 'undefined' && module.exports) {
    module.exports = InboxWebSocket;
}
window.InboxWebSocket = InboxWebSocket;
