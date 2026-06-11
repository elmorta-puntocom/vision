USE vision_db;

CREATE TABLE IF NOT EXISTS dispositivos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    device_id VARCHAR(64) NOT NULL UNIQUE,
    mac VARCHAR(32) NULL,
    device_secret VARCHAR(128) NOT NULL,
    activation_code_hash VARCHAR(255) NOT NULL,
    usuario_id INT NULL,
    ip_address VARCHAR(45) NULL,
    firmware_version VARCHAR(32) NULL,
    last_nonce VARCHAR(64) NULL,
    last_seen DATETIME NULL,
    linked_at DATETIME NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_dispositivos_usuario_id (usuario_id),
    CONSTRAINT fk_dispositivos_usuario
        FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS dispositivo_eventos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    dispositivo_id INT NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    value VARCHAR(120) NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_dispositivo_eventos_dispositivo_id (dispositivo_id),
    CONSTRAINT fk_dispositivo_eventos_dispositivo
        FOREIGN KEY (dispositivo_id) REFERENCES dispositivos(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS dispositivo_comandos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    dispositivo_id INT NOT NULL,
    command VARCHAR(50) NOT NULL,
    consumed TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    consumed_at DATETIME NULL,
    INDEX idx_dispositivo_comandos_dispositivo_id (dispositivo_id),
    INDEX idx_dispositivo_comandos_consumed (consumed),
    CONSTRAINT fk_dispositivo_comandos_dispositivo
        FOREIGN KEY (dispositivo_id) REFERENCES dispositivos(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
