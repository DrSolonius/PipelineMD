# pipelineMD

Pipeline local para preparar paquetes CHARMM-GUI, organizar proyectos y
simulaciones, ejecutar NAMD localmente o por SSH/SLURM y visualizar métricas en
tiempo real.

## Desarrollo

Requisitos: Python 3.11+, Node.js 22+, npm y, para ejecución local en Windows,
WSL con NAMD y csh.

```powershell
cd dashboard
npm install
npm run dev
```

El panel queda disponible en `http://localhost:3000` y la API local en
`http://127.0.0.1:8765`.

Los proyectos, simulaciones, paquetes `.tgz`, resultados, conexiones SSH y
credenciales son datos locales y no se incluyen en Git.

## Validación

```powershell
python -m unittest discover -s dashboard/scripts -p "test_*.py"
cd dashboard
npm run build
```
