# Destino de ejecución

## Proyectos y simulaciones

Cada proyecto tiene un nombre, una descripción y una colección de simulaciones.
Puedes modificar el nombre y la descripción con «Editar proyecto» sin cambiar
sus simulaciones. El nombre admite 100 caracteres y la descripción 4000.

Primero crea o selecciona un proyecto en «Proyectos y simulaciones». Después usa
«Cargar CHARMM-GUI» para agregar un `.tgz` o `.tar.gz`. Cada carga correcta crea
una simulación independiente dentro de ese proyecto, incluso si el archivo tiene
el mismo nombre que una carga anterior. Selecciona una simulación para ver sus
métricas y ejecutar su preparación en Local o SSH.

Al crear un proyecto se abre el selector de carpetas de Windows. En la ubicación
elegida se crea `<nombre>-<id>/project.json` y `simulations/`. Cada simulación
conserva el archivo original en `charmm-gui/`, usa `work/replica_01/namd/` para
los archivos preparados y crea `results/` para sus resultados y análisis.

El catálogo se guarda en `config/projects.json`. Las nuevas carpetas siguen
`simulations/projects/<proyecto-id>/<simulacion-id>/replica_01/namd`.
Las carpetas del formato anterior aparecen en «Simulaciones anteriores» sin
moverse. La selección de proyecto/simulación queda bloqueada durante una carga
o ejecución iniciada desde ese panel.

API: `GET /projects`, `POST /projects` con `{ "name": "Nombre", "description": "Descripción" }`,
`POST /update-project` con `projectId`, `name` y `description`,
`POST /upload` con cabeceras `X-Filename` y `X-Project-Id`, y
`POST /select-simulation` con `projectId` y `simulationId`.
`POST /run-preparation` ahora recibe `projectId`, `simulationId` y `targetId`;
la ruta se obtiene del catálogo, no de una ruta enviada por el navegador.

## Ejecución

En Resumen, el módulo «Dónde correr la simulación» permite elegir Local o una
conexión guardada en SSH. Pulsa Actualizar después de agregar o probar un servidor.
RUN preparación envía el destino elegido a la API y ejecuta README_preparacion.
La selección se bloquea mientras corre la preparación. Una nueva sesión comienza
con Local seleccionado.

- Local: usa WSL en Windows, o sh en Linux. Requiere NAMD y csh.
- SSH directo: agrega y prueba la conexión en SSH. Requiere namd3, csh, tar y
  setsid disponibles en el PATH de la sesión SSH no interactiva. Usa las claves
  conocidas por OpenSSH y autenticación no interactiva por agente o archivo.
- Slurm/PBS: aparecen como no disponibles; aún no se envían trabajos a colas.

Cada ejecución SSH crea `<directorio remoto>/runs/<id>` y transfiere los archivos
de entrada. La API comprueba el entorno remoto antes de archivar las salidas
locales anteriores. Los .out de step6 se copian periódicamente al equipo local
para alimentar las métricas. Trayectorias y reinicios permanecen en el servidor:
RMSD/RMSF de la nueva trayectoria remota no están disponibles automáticamente.
La consola muestra la ruta de los resultados. STOP usa el destino guardado al
lanzar y termina el grupo de procesos de esa ejecución remota.

Mantén la API activa durante la ejecución. El registro de procesos está en memoria;
no hay recuperación automática de trabajos remotos después de reiniciar la API.
Un corte de SSH puede dejar un proceso remoto activo: comprueba el directorio de
la corrida en el servidor antes de volver a lanzar.

Después de actualizar este código, reinicia la API cuando no haya simulaciones
activas y recarga el panel. Inicio habitual desde dashboard: `npm run dev`.

Validación: `python -m unittest discover -s dashboard/scripts -p test_execution_targets.py -v`
desde la raíz del proyecto; TypeScript y `npm run build` desde dashboard.
