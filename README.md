# pipelineMD

Pipeline local para preparar paquetes CHARMM-GUI, organizar proyectos y
simulaciones, ejecutar NAMD localmente o por SSH/SLURM y visualizar métricas en
tiempo real.

## Desarrollo

Requisitos: Python 3.11+, Node.js 22+, npm y, para ejecución local en Windows,
WSL con NAMD y csh.

Instala las dependencias del frontend una sola vez:

```powershell
cd dashboard
npm install
```

Para levantar los servicios por separado, abre dos terminales desde la raíz
del repositorio:

```powershell
# Terminal 1: backend / API local
cd dashboard
python scripts/upload_server.py
```

```powershell
# Terminal 2: frontend
cd dashboard
npm run dev:web
```

También puedes iniciar ambos procesos juntos con:

```powershell
cd dashboard
npm run dev
```

El frontend queda disponible en `http://localhost:3000` y la API local en
`http://127.0.0.1:8765`.

Los proyectos, simulaciones, paquetes `.tgz`, resultados, conexiones SSH y
credenciales son datos locales y no se incluyen en Git.

Cada proyecto administrado incluye un `config.ini` portable. El archivo registra
las simulaciones, rutas relativas, etapa actual, etapas terminadas, pendientes y
fallidas, porcentaje de avance y la simulación activa del dashboard. La opción
«Cargar proyecto desde config.ini» permite volver a registrar una carpeta de
proyecto aunque el catálogo SQLite local no esté disponible.

Al correr nuevamente una simulación cargada, el sistema valida los marcadores
de término de NAMD y genera `README_reanudar`: las etapas finalizadas
correctamente se omiten y la ejecución continúa desde la primera pendiente.
Los proyectos de versiones anteriores se migran automáticamente a `config.ini`
la primera vez que aparecen en el dashboard; sus resultados NAMD no se mueven.

## Validación

```powershell
python -m unittest discover -s dashboard/scripts -p "test_*.py"
cd dashboard
npm run build
```
