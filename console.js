// SPARK Developer Console Frontend Controller

document.addEventListener('DOMContentLoaded', () => {
  // State management
  let serverManaged = false;
  let needsAccessCode = false;
  let activeTab = 'dashboard';
  let dashboardPollInterval = null;
  let videoPollInterval = null;

  // Active Filters State for Marketplace
  const activeFilters = {
    search: '',
    provider: 'all',
    type: 'all',
    tag: 'all',
    billing: 'all'
  };

  // Cache DOM elements
  const navItems = document.querySelectorAll('.nav-item');
  const tabContents = document.querySelectorAll('.tab-content');
  const pageTitleLabel = document.getElementById('page-title-label');
  const localTokenInput = document.getElementById('local-token-input');
  
  // Status badges
  const statusGatewayDot = document.getElementById('status-gateway-dot');
  const statusGatewayText = document.getElementById('status-gateway-text');
  const statusAppDot = document.getElementById('status-app-dot');
  const statusAppText = document.getElementById('status-app-text');
  const managedModeBadge = document.getElementById('managed-mode-badge');
  const managedModeText = document.getElementById('managed-mode-text');

  // Stats
  const statActiveTasks = document.getElementById('stat-active-tasks');
  const statRateMax = document.getElementById('stat-rate-max');
  const statServerMode = document.getElementById('stat-server-mode');
  const taskTableBody = document.getElementById('task-table-body');
  const taskMonitorSyncTime = document.getElementById('task-monitor-sync-time');

  // Marketplace Elements
  const searchModelInput = document.getElementById('search-model-input');
  const filterProviders = document.querySelectorAll('#filter-provider .filter-item');
  const filterTypes = document.querySelectorAll('#filter-type .filter-item');
  const filterTags = document.querySelectorAll('#filter-tags .filter-item');
  const filterBillings = document.querySelectorAll('#filter-billing .filter-item');
  const btnResetAll = document.getElementById('btn-reset-all');
  
  // ==========================================================================
  // Dynamic Model Marketplace Data & Rendering (P2 Optimization)
  // ==========================================================================
  const MODELS_DATA = [
    {
        "name": "doubao-seedance-2-0-260128",
        "displayName": "doubao-seedance-2-0-260128",
        "provider": "Doubao",
        "providerDisplay": "Doubao (豆包)",
        "type": "video",
        "tagsAttr": "视频,多模态,参考图",
        "tags": [
            "视频",
            "多模态",
            "参考图"
        ],
        "bannerClass": "banner-doubao-pro",
        "badgeStyle": "background: rgba(138, 43, 226, 0.15); color: #a78bfa; border-color: rgba(138, 43, 226, 0.25);",
        "badgeText": "视频",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <circle cx=\"50\" cy=\"50\" r=\"30\" stroke=\"rgba(167, 139, 250, 0.5)\" stroke-width=\"3\"/>\n                    <circle cx=\"50\" cy=\"50\" r=\"10\" stroke=\"rgba(167, 139, 250, 0.4)\" stroke-width=\"2\"/>\n                    <circle cx=\"50\" cy=\"32\" r=\"6\" fill=\"rgba(167, 139, 250, 0.3)\"/>\n                    <circle cx=\"50\" cy=\"68\" r=\"6\" fill=\"rgba(167, 139, 250, 0.3)\"/>\n                    <circle cx=\"32\" cy=\"50\" r=\"6\" fill=\"rgba(167, 139, 250, 0.3)\"/>\n                    <circle cx=\"68\" cy=\"50\" r=\"6\" fill=\"rgba(167, 139, 250, 0.3)\"/>\n                  </svg>",
        "description": "豆包视频生成旗舰模型（多模态参考版）。支持输入图片、视频等多模态素材进行精准控制，实现具有高度物理一致性与连贯运镜的炫酷 CG 动画合成。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 9.0000/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 45.0000/M"
            }
        ],
        "billing": "",
        "tryNowType": "video"
    },
    {
        "name": "doubao-seedance-2-0",
        "displayName": "doubao-seedance-2-0",
        "provider": "Doubao",
        "providerDisplay": "Doubao (豆包)",
        "type": "video",
        "tagsAttr": "视频,标准版",
        "tags": [
            "视频",
            "标准版"
        ],
        "bannerClass": "banner-doubao-turbo",
        "badgeStyle": "background: rgba(138, 43, 226, 0.15); color: #a78bfa; border-color: rgba(138, 43, 226, 0.25);",
        "badgeText": "视频",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <circle cx=\"50\" cy=\"50\" r=\"28\" stroke=\"rgba(79, 172, 254, 0.5)\" stroke-width=\"3\"/>\n                    <polygon points=\"42,35 68,50 42,65\" fill=\"rgba(79, 172, 254, 0.4)\" stroke=\"rgba(79, 172, 254, 0.6)\" stroke-width=\"2\" stroke-linejoin=\"round\"/>\n                  </svg>",
        "description": "豆包视频生成标准版模型。拥有优秀的运动物理合理性与极佳画质表现，适合高保真度、中等通量吞吐的通用视频创意与时光镜转场生成。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 4.5000/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 22.5000/M"
            }
        ],
        "billing": "",
        "tryNowType": "video"
    },
    {
        "name": "doubao-seedance-2-0-fast",
        "displayName": "doubao-seedance-2-0-fast",
        "provider": "Doubao",
        "providerDisplay": "Doubao (豆包)",
        "type": "video",
        "tagsAttr": "视频,极速",
        "tags": [
            "视频",
            "极速"
        ],
        "bannerClass": "banner-doubao-evolving",
        "badgeStyle": "background: rgba(138, 43, 226, 0.15); color: #a78bfa; border-color: rgba(138, 43, 226, 0.25);",
        "badgeText": "视频",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <rect x=\"25\" y=\"38\" width=\"50\" height=\"34\" rx=\"3\" fill=\"rgba(0, 242, 254, 0.12)\" stroke=\"rgba(0, 242, 254, 0.4)\" stroke-width=\"2\"/>\n                    <path d=\"M25 46 L75 46\" stroke=\"rgba(0, 242, 254, 0.4)\" stroke-width=\"2\"/>\n                    <path d=\"M35 38 L42 46\" stroke=\"rgba(0, 242, 254, 0.4)\" stroke-width=\"1.5\"/>\n                    <path d=\"M51 38 L58 46\" stroke=\"rgba(0, 242, 254, 0.4)\" stroke-width=\"1.5\"/>\n                    <path d=\"M67 38 L74 46\" stroke=\"rgba(0, 242, 254, 0.4)\" stroke-width=\"1.5\"/>\n                  </svg>",
        "description": "豆包视频生成极速版模型。具有超高的生成响应速度与高性价比表现，适用于需要高吞吐、高效率批量生成的极速视频合成场景。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 2.0000/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 10.0000/M"
            }
        ],
        "billing": "",
        "tryNowType": "video"
    },
    {
        "name": "gemini-3.5-flash",
        "displayName": "gemini-3.5-flash",
        "provider": "Google",
        "providerDisplay": "Google",
        "type": "text",
        "tagsAttr": "对话,工具,识图",
        "tags": [
            "对话",
            "工具",
            "识图"
        ],
        "bannerClass": "banner-gemini-flash",
        "badgeStyle": "",
        "badgeText": "文本",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <circle cx=\"50\" cy=\"50\" r=\"6\" fill=\"rgba(255, 255, 255, 0.6)\"/>\n                    <line x1=\"50\" y1=\"50\" x2=\"20\" y2=\"20\" stroke=\"rgba(255, 255, 255, 0.3)\" stroke-width=\"1.5\"/>\n                    <line x1=\"50\" y1=\"50\" x2=\"80\" y2=\"20\" stroke=\"rgba(255, 255, 255, 0.3)\" stroke-width=\"1.5\"/>\n                    <line x1=\"50\" y1=\"50\" x2=\"20\" y2=\"80\" stroke=\"rgba(255, 255, 255, 0.3)\" stroke-width=\"1.5\"/>\n                    <line x1=\"50\" y1=\"50\" x2=\"80\" y2=\"80\" stroke=\"rgba(255, 255, 255, 0.3)\" stroke-width=\"1.5\"/>\n                    <line x1=\"50\" y1=\"50\" x2=\"50\" y2=\"10\" stroke=\"rgba(255, 255, 255, 0.3)\" stroke-width=\"1.5\"/>\n                    <line x1=\"50\" y1=\"50\" x2=\"50\" y2=\"90\" stroke=\"rgba(255, 255, 255, 0.3)\" stroke-width=\"1.5\"/>\n                    <line x1=\"50\" y1=\"50\" x2=\"10\" y2=\"50\" stroke=\"rgba(255, 255, 255, 0.3)\" stroke-width=\"1.5\"/>\n                    <line x1=\"50\" y1=\"50\" x2=\"90\" y2=\"50\" stroke=\"rgba(255, 255, 255, 0.3)\" stroke-width=\"1.5\"/>\n                  </svg>",
        "description": "Gemini 3.5 Flash 已经正式发布（GA），性能稳定，可大规模用于生产环境。作为轻量智能体、编码和长期任务的高性价比方案提供领先性能。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 2.2500/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 13.5000/M"
            }
        ],
        "billing": "",
        "tryNowType": "text"
    },
    {
        "name": "gpt-image-2",
        "displayName": "gpt-image-2",
        "provider": "OpenAI",
        "providerDisplay": "OpenAI",
        "type": "image",
        "tagsAttr": "绘画,dall-e-3格式",
        "tags": [
            "绘画",
            "dall-e-3格式"
        ],
        "bannerClass": "banner-gpt-image",
        "badgeStyle": "",
        "badgeText": "图像",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <rect x=\"25\" y=\"25\" width=\"50\" height=\"40\" rx=\"3\" fill=\"rgba(255, 118, 177, 0.15)\" stroke=\"rgba(255, 118, 177, 0.4)\" stroke-width=\"2\"/>\n                    <circle cx=\"40\" cy=\"38\" r=\"4\" fill=\"rgba(255, 118, 177, 0.4)\"/>\n                    <path d=\"M28 60 L45 45 L55 52 L72 38\" stroke=\"rgba(255, 118, 177, 0.5)\" stroke-width=\"2\" stroke-linejoin=\"round\"/>\n                  </svg>",
        "description": "GPT Image 2 是我们最先进的图像生成模型，支持快速、高质量的图像生成和编辑，它支持灵活的图像尺寸和高保真图像输入。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 3.0000/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 18.0000/M"
            }
        ],
        "billing": "",
        "tryNowType": "image"
    },
    {
        "name": "gpt-4o",
        "displayName": "gpt-4o",
        "provider": "OpenAI",
        "providerDisplay": "OpenAI",
        "type": "text",
        "tagsAttr": "对话,工具,识图,多模态",
        "tags": [
            "对话",
            "工具",
            "识图",
            "多模态"
        ],
        "bannerClass": "banner-gpt",
        "badgeStyle": "background: rgba(16, 163, 127, 0.15); color: #10a37f; border-color: rgba(16, 163, 127, 0.25);",
        "badgeText": "文本",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <circle cx=\"50\" cy=\"50\" r=\"28\" stroke=\"rgba(16, 163, 127, 0.4)\" stroke-width=\"2\"/>\n                    <path d=\"M50 30 C 45 40, 55 40, 50 50 C 45 60, 55 60, 50 70\" stroke=\"rgba(16, 163, 127, 0.5)\" stroke-width=\"2\" stroke-linecap=\"round\"/>\n                    <path d=\"M30 50 C 40 45, 40 55, 50 50 C 60 45, 60 55, 70 50\" stroke=\"rgba(16, 163, 127, 0.5)\" stroke-width=\"2\" stroke-linecap=\"round\"/>\n                  </svg>",
        "description": "OpenAI 旗舰级多模态大语言模型。具有出色的跨文本、多模态识图与物理逻辑推理能力，能高效处理复杂创意构思、长文本重写及高级工具调用任务。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 15.0000/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 60.0000/M"
            }
        ],
        "billing": "",
        "tryNowType": "text"
    },
    {
        "name": "gpt-4o-mini",
        "displayName": "gpt-4o-mini",
        "provider": "OpenAI",
        "providerDisplay": "OpenAI",
        "type": "text",
        "tagsAttr": "对话,工具,识图,多模态",
        "tags": [
            "对话",
            "工具",
            "识图",
            "多模态"
        ],
        "bannerClass": "banner-gpt-mini",
        "badgeStyle": "background: rgba(16, 163, 127, 0.15); color: #10a37f; border-color: rgba(16, 163, 127, 0.25);",
        "badgeText": "文本",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <circle cx=\"50\" cy=\"50\" r=\"22\" stroke=\"rgba(16, 163, 127, 0.3)\" stroke-width=\"1.5\"/>\n                    <circle cx=\"50\" cy=\"50\" r=\"8\" fill=\"rgba(16, 163, 127, 0.2)\"/>\n                  </svg>",
        "description": "OpenAI 轻量级高性价比多模态模型。在保持极速响应与超低调用成本的同时，提供优异 of 对话编写、长文本对齐和基础多模态识图能力。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 0.7500/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 3.0000/M"
            }
        ],
        "billing": "",
        "tryNowType": "text"
    },
    {
        "name": "gemini-3-flash-agent",
        "displayName": "gemini-3-flash-agent",
        "provider": "Google",
        "providerDisplay": "Google",
        "type": "text",
        "tagsAttr": "对话,工具,思考",
        "tags": [
            "对话",
            "工具",
            "思考"
        ],
        "bannerClass": "banner-gemini-agent",
        "badgeStyle": "",
        "badgeText": "文本",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <rect x=\"20\" y=\"20\" width=\"60\" height=\"60\" rx=\"4\" stroke=\"rgba(0, 242, 254, 0.3)\" stroke-dasharray=\"4 4\" stroke-width=\"1.5\"/>\n                    <line x1=\"20\" y1=\"40\" x2=\"80\" y2=\"40\" stroke=\"rgba(0, 242, 254, 0.2)\" stroke-width=\"1\"/>\n                    <line x1=\"20\" y1=\"60\" x2=\"80\" y2=\"60\" stroke=\"rgba(0, 242, 254, 0.2)\" stroke-width=\"1\"/>\n                    <line x1=\"40\" y1=\"20\" x2=\"40\" y2=\"80\" stroke=\"rgba(0, 242, 254, 0.2)\" stroke-width=\"1\"/>\n                    <line x1=\"60\" y1=\"20\" x2=\"60\" y2=\"80\" stroke=\"rgba(0, 242, 254, 0.2)\" stroke-width=\"1\"/>\n                    <circle cx=\"50\" cy=\"50\" r=\"10\" fill=\"rgba(138, 43, 226, 0.25)\" stroke=\"var(--primary)\" stroke-width=\"1.5\"/>\n                  </svg>",
        "description": "系统默认的推理规划智能体模型。用于快速对齐设计维度，构建极高一致性的渲染指令时间轴序列（SCUP 编排）。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 1.5000/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 7.5000/M"
            }
        ],
        "billing": "",
        "tryNowType": "text"
    },
    {
        "name": "gemini-3.1-flash-image",
        "displayName": "gemini-3.1-flash-image",
        "provider": "Google",
        "providerDisplay": "Google",
        "type": "image",
        "tagsAttr": "绘画,参考图,识图",
        "tags": [
            "绘画",
            "参考图",
            "识图"
        ],
        "bannerClass": "banner-gemini-image",
        "badgeStyle": "",
        "badgeText": "图像",
        "illustration": "<svg class=\"banner-illustration\" viewBox=\"0 0 100 100\" fill=\"none\" xmlns=\"http://www.w3.org/2000/svg\">\n                    <path d=\"M20 70 C 40 30, 60 30, 80 70\" stroke=\"url(#paintGrad)\" stroke-width=\"8\" stroke-linecap=\"round\"/>\n                    <path d=\"M20 70 L25 80 L15 80 Z\" fill=\"#fff\"/>\n                    <defs>\n                      <linearGradient id=\"paintGrad\" x1=\"20\" y1=\"50\" x2=\"80\" y2=\"50\" gradientUnits=\"userSpaceOnUse\">\n                        <stop offset=\"0%\" stop-color=\"#ff007f\"/>\n                        <stop offset=\"50%\" stop-color=\"#8a2be2\"/>\n                        <stop offset=\"100%\" stop-color=\"#00f2fe\"/>\n                      </linearGradient>\n                    </defs>\n                  </svg>",
        "description": "核心多模态图像生成与图生图编辑底座（界面别名 nano-banana-2）。支持输入单张或多张材质/风格参考图实现高画质局部渲染。",
        "pricing": [
            {
                "label": "输入价格",
                "value": "⚡ 2.5000/M"
            },
            {
                "label": "补全价格",
                "value": "⚡ 15.0000/M"
            }
        ],
        "billing": "● 按量计费",
        "tryNowType": "image"
    }
];

  function renderMarketCards() {
    const grid = document.getElementById('market-model-grid');
    if (!grid) return;
    grid.innerHTML = '';
    
    MODELS_DATA.forEach(model => {
      const card = document.createElement('div');
      card.className = 'market-card';
      card.setAttribute('data-provider', model.provider);
      card.setAttribute('data-type', model.type);
      card.setAttribute('data-name', model.name);
      card.setAttribute('data-tags', model.tagsAttr);
      
      // Determine badge class to avoid inline styles
      let badgeClass = 'banner-badge';
      if (model.badgeStyle) {
        if (model.provider === 'Doubao') badgeClass += ' badge-doubao-video';
        else if (model.provider === 'OpenAI' && model.type === 'text') badgeClass += ' badge-gpt-text';
      }
      
      card.innerHTML = `
        <div class="card-banner ${model.bannerClass}">
          <span class="${badgeClass}">${model.badgeText}</span>
          ${model.illustration}
        </div>
        <div class="card-body">
          <div class="card-model-info">
            <h3 class="card-model-name">${model.displayName}</h3>
            <span class="card-provider">${model.providerDisplay}</span>
          </div>
          <p class="card-desc">${model.description}</p>
          
          <div class="card-pricing">
            ${model.pricing.map(p => `
              <div class="price-row">
                <span class="price-label">${p.label}</span>
                <span class="price-value">${p.value}</span>
              </div>
            `).join('')}
          </div>

          <div class="card-tags-row">
            <div class="tag-pills">
              ${model.tags.map(tag => `<span class="pill">${tag}</span>`).join('')}
            </div>
            ${model.billing ? `<span class="billing-badge">${model.billing}</span>` : ''}
          </div>
        </div>
        <div class="card-footer">
          <button class="btn-icon-only btn-copy-id" data-id="${model.name}" title="复制模型 ID">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 0 1-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H5.25m11.9-3.664A2.251 2.251 0 0 0 15 2.25h-1.5a2.25 2.25 0 0 0-2.25 2.25h-.007a2.25 2.25 0 0 0-2.25 2.25M18.75 8.25v11.25m0-11.25a2.25 2.25 0 0 0-2.25-2.25h-1.5a2.25 2.25 0 0 0-2.25 2.25m7.5 0v11.25m-18-11.25a2.25 2.25 0 0 1 2.25-2.25h1.5a2.25 2.25 0 0 1 2.25 2.25m-4.5 0v11.25m11.9-3.664C19.302 15.1 19.5 14.201 19.5 13.25V9.75" />
            </svg>
          </button>
          <button class="btn btn-primary btn-try-now" data-id="${model.name}" data-type="${model.tryNowType}">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.5" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
            </svg>
            <span>立即体验</span>
          </button>
        </div>
      `;
      grid.appendChild(card);
    });
  }

  function updateFilterBadges() {
    const totalCount = MODELS_DATA.length;
    const providerCounts = { all: totalCount };
    const typeCounts = { all: totalCount };
    const tagCounts = { all: totalCount };
    const billingCounts = { all: totalCount, payg: totalCount };
    
    MODELS_DATA.forEach(model => {
      providerCounts[model.provider] = (providerCounts[model.provider] || 0) + 1;
      typeCounts[model.type] = (typeCounts[model.type] || 0) + 1;
      model.tags.forEach(tag => {
        tagCounts[tag] = (tagCounts[tag] || 0) + 1;
      });
    });
    
    const updateCategoryBadges = (selector, countsMap) => {
      document.querySelectorAll(selector).forEach(item => {
        const val = item.getAttribute('data-value');
        const badge = item.querySelector('.count-badge');
        if (badge) {
          badge.textContent = countsMap[val] || 0;
        }
      });
    };
    
    updateCategoryBadges('#filter-provider .filter-item', providerCounts);
    updateCategoryBadges('#filter-type .filter-item', typeCounts);
    updateCategoryBadges('#filter-tags .filter-item', tagCounts);
    updateCategoryBadges('#filter-billing .filter-item', billingCounts);
  }

  const marketModelGrid = document.getElementById('market-model-grid');
  
  // Render marketplace cards dynamically and calculate filter counts
  renderMarketCards();
  updateFilterBadges();
  
  let marketCards = document.querySelectorAll('.market-card');

  // Suffix Generator
  const suffixBaseModel = document.getElementById('suffix-base-model');
  const suffixRatio = document.getElementById('suffix-ratio');
  const suffixQuality = document.getElementById('suffix-quality');
  const generatedModelId = document.getElementById('generated-model-id');
  const generatedModelPixels = document.getElementById('generated-model-pixels');
  const btnCopyModelId = document.getElementById('btn-copy-model-id');

  // Sandbox Playground
  const playEndpoint = document.getElementById('play-endpoint');
  const playBody = document.getElementById('play-body');
  const playFilesGroup = document.getElementById('play-files-group');
  const imageUploadZone = document.getElementById('image-upload-zone');
  const sandboxImageInput = document.getElementById('sandbox-image-input');
  const sandboxPreviewList = document.getElementById('sandbox-preview-list');
  const btnSendRequest = document.getElementById('btn-send-request');
  const btnClearConsole = document.getElementById('btn-clear-console');
  const playConsoleLog = document.getElementById('play-console-log');
  const playPreviewPanel = document.getElementById('play-preview-panel');
  const playPreviewImg = document.getElementById('play-preview-img');
  const btnDownloadImg = document.getElementById('btn-download-img');

  // 1. Initial Setup
  const init = () => {
    // Load local token from localStorage
    const savedToken = localStorage.getItem('spark_dev_token');
    if (savedToken) {
      localTokenInput.value = savedToken;
    }

    // Bind token input changes
    localTokenInput.addEventListener('input', (e) => {
      localStorage.setItem('spark_dev_token', e.target.value.trim());
    });

    // Toggle token visibility
    const tokenToggleBtn = document.getElementById('token-toggle-btn');
    const tokenToggleIcon = document.getElementById('token-toggle-icon');
    if (tokenToggleBtn && tokenToggleIcon) {
      tokenToggleBtn.addEventListener('click', () => {
        if (localTokenInput.type === 'password') {
          localTokenInput.type = 'text';
          // Change to Eye Slash icon
          tokenToggleIcon.innerHTML = `
            <path stroke-linecap="round" stroke-linejoin="round" d="M3.98 8.223A10.477 10.477 0 0 0 1.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.451 10.451 0 0 1 12 4.5c4.756 0 8.773 3.162 10.065 7.498a10.522 10.522 0 0 1-4.293 5.774M6.228 6.228 3 3m3.228 3.228 3.65 3.65m7.815 7.815 3 3m-3-3a3 3 0 0 1-4.243-4.243m0 0-3.65-3.65m0 0a3 3 0 0 1 4.243 4.243m-4.243-4.243L16.5 16.5" />
          `;
        } else {
          localTokenInput.type = 'password';
          // Change back to Eye icon
          tokenToggleIcon.innerHTML = `
            <path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 0 1 0-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178Z" />
            <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
          `;
        }
      });
    }

    // Copy tunnel URL
    const copyTunnelBtn = document.getElementById('btn-copy-tunnel-url');
    const tunnelUrlCode = document.getElementById('tunnel-url-code');
    if (copyTunnelBtn && tunnelUrlCode) {
      copyTunnelBtn.addEventListener('click', () => {
        const urlText = tunnelUrlCode.textContent.trim();
        if (urlText && urlText !== '-') {
          navigator.clipboard.writeText(urlText).then(() => {
            const originalText = copyTunnelBtn.innerHTML;
            copyTunnelBtn.innerHTML = `
              <span style="color:var(--success)">✓ 已复制</span>
            `;
            setTimeout(() => {
              copyTunnelBtn.innerHTML = originalText;
            }, 1500);
          });
        }
      });
    }

    // Clear system cache in DevPortal
    const btnClearSystemCache = document.getElementById('btn-clear-system-cache');
    if (btnClearSystemCache) {
      btnClearSystemCache.addEventListener('click', async () => {
        if (!confirm('确定要清理系统缓存（packet_cache.json）吗？这会清除已缓存的 LLM 激发数据。')) {
          return;
        }
        try {
          btnClearSystemCache.disabled = true;
          btnClearSystemCache.textContent = '清理中...';
          const resp = await fetch('/api/clear-cache', { method: 'POST' });
          const data = await resp.json();
          if (data.status === 'success') {
            alert('系统缓存清理成功！');
            checkSystemStatuses();
          } else {
            alert('清理失败: ' + data.message);
          }
        } catch (err) {
          alert('请求出错: ' + err.message);
        } finally {
          btnClearSystemCache.disabled = false;
          btnClearSystemCache.textContent = '清理缓存';
        }
      });
    }

    // Start background status checks
    checkSystemStatuses();
    dashboardPollInterval = setInterval(checkSystemStatuses, 3000);

    // Setup Marketplace filter bindings
    setupMarketplaceFilters();

    // Initial model suffix calculation
    calculateModelSuffix();
    
    // Bind playground templates
    updatePlaygroundTemplate();
    
    // Bind documentation panels
    renderDocSnippets();

    // Wire visual uploader event listeners
    setupVisualUploader();
  };

  // 2. Tab Navigation
  navItems.forEach(item => {
    item.addEventListener('click', () => {
      const targetTab = item.getAttribute('data-tab');
      swapTab(targetTab);
    });
  });

  function swapTab(targetTab) {
    if (targetTab === activeTab) return;

    // Update active nav item
    navItems.forEach(nav => {
      if (nav.getAttribute('data-tab') === targetTab) {
        nav.classList.add('active');
      } else {
        nav.classList.remove('active');
      }
    });

    // Swap tab visibility
    tabContents.forEach(content => content.classList.remove('active'));
    const activeContent = document.getElementById(`tab-${targetTab}`);
    if (activeContent) {
      activeContent.classList.add('active');
    }

    activeTab = targetTab;
    
    // Update header title
    const labelMap = {
      'dashboard': '控制台仪表盘',
      'models': '可用模型中心',
      'docs': 'API 交互文档',
      'playground': '接口测试沙盒'
    };
    pageTitleLabel.textContent = labelMap[activeTab] || '开发者中心';
  }

  // 3. Status Checking & Dynamic Dashboard
  async function checkSystemStatuses() {
    try {
      const modeResp = await fetch('/api/mode');
      if (!modeResp.ok) throw new Error('API server unreachable');
      const modeData = await modeResp.json();
      
      serverManaged = modeData.server_managed;
      needsAccessCode = modeData.needs_access_code;

      if (serverManaged) {
        statServerMode.textContent = '托管模式 (Managed)';
        managedModeBadge.style.background = 'rgba(138, 43, 226, 0.15)';
        managedModeBadge.style.borderColor = 'var(--secondary)';
        managedModeText.innerHTML = '<span style="color:#a78bfa">● 托管模式 (Managed)</span>';
        
        localTokenInput.placeholder = '当前为服务端托管模式，密钥从服务端加载';
        localTokenInput.disabled = true;
      } else {
        statServerMode.textContent = '本地模式 (Local)';
        managedModeBadge.style.background = 'rgba(255, 255, 255, 0.03)';
        managedModeBadge.style.borderColor = 'var(--border-color)';
        managedModeText.innerHTML = '<span>● 本地模式 (Local)</span>';
        localTokenInput.placeholder = '输入 User Token 进行测试...';
        localTokenInput.disabled = false;
      }

      const devToken = localTokenInput.value.trim();
      const configPayload = {
        config: {
          baseUrl: 'http://127.0.0.1:8046/v1',
          apiKey: serverManaged ? '' : devToken
        }
      };

      const pingResp = await fetch('/api/ping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(configPayload)
      });
      
      const pingData = await pingResp.json();
      if (pingData.online) {
        statusGatewayDot.className = 'status-dot online';
        statusGatewayText.textContent = '在线 (8046)';
      } else {
        statusGatewayDot.className = 'status-dot offline';
        statusGatewayText.textContent = '无法连接到本地网关';
      }

      statusAppDot.className = 'status-dot online';
      statusAppText.textContent = '在线 (8085)';

      const tasksResp = await fetch('/api/tasks');
      if (tasksResp.ok) {
        const tasks = await tasksResp.json();
        const runningTasks = tasks.filter(t => t.status === 'running');
        statActiveTasks.textContent = runningTasks.length;
        renderTasksTable(tasks);
      }
      
      statRateMax.textContent = '20';

      // Get cache info
      try {
        const cacheResp = await fetch('/api/cache-info');
        if (cacheResp.ok) {
          const cacheData = await cacheResp.json();
          const sizeKb = (cacheData.packet_cache_size / 1024).toFixed(2);
          const keysCount = cacheData.packet_cache_keys;
          const statCacheSize = document.getElementById('stat-cache-size');
          if (statCacheSize) {
            statCacheSize.textContent = `${sizeKb} KB (${keysCount}项)`;
          }
        }
      } catch (cacheErr) {
        console.warn('Failed to fetch cache info', cacheErr);
      }

    } catch (err) {
      statusAppDot.className = 'status-dot offline';
      statusAppText.textContent = '已断开连接';
      statusGatewayDot.className = 'status-dot offline';
      statusGatewayText.textContent = '未就绪';
      statServerMode.textContent = '未知';
    }
  }

  function renderTasksTable(tasks) {
    taskMonitorSyncTime.textContent = `上次更新: ${new Date().toLocaleTimeString()}`;
    
    if (!tasks || tasks.length === 0) {
      taskTableBody.innerHTML = `
        <tr>
          <td colspan="6" style="text-align: center; color: var(--text-muted); padding: 40px 0;">
            目前没有正在运行或历史生成任务。
          </td>
        </tr>
      `;
      return;
    }

    taskTableBody.innerHTML = tasks.map(t => {
      const theme = t.dimensions ? (t.dimensions.theme || '未指定主题') : '应用内直呼生成';
      const duration = t.result && t.result.timings ? `${t.result.timings.total_duration_seconds}s` : '-';
      const beats = t.dimensions ? (t.dimensions.beats_count || 15) : '-';
      
      let themeDisplay = theme;
      if (t.result && t.result.token_usage) {
        const usage = t.result.token_usage;
        themeDisplay += `<div style="font-size: 11px; color: var(--text-muted); margin-top: 4px; font-family: var(--font-mono, monospace);">` +
                       `Tokens: ${usage.total_tokens} (I:${usage.prompt_tokens} O:${usage.completion_tokens}) | Calls: ${usage.api_calls}` +
                       `</div>`;
      }
      
      let statusBadge = '';
      if (t.status === 'running') {
        statusBadge = '<span class="badge badge-running">● 正在合成</span>';
      } else if (t.status === 'completed') {
        statusBadge = '<span class="badge badge-completed">● 已完成</span>';
      } else {
        statusBadge = `<span class="badge badge-failed" title="${t.error || ''}">● 失败</span>`;
      }

      let actionButtons = '';
      if (t.status === 'running') {
        actionButtons = `<button class="btn btn-sm btn-danger" onclick="cancelTask('${t.id}')">中止</button>`;
      } else {
        actionButtons = `<button class="btn btn-sm" style="color:#f87171; border-color:transparent;" onclick="deleteTask('${t.id}')">删除</button>`;
      }

      return `
        <tr>
          <td style="font-family: var(--font-mono); font-size:13px; color: var(--primary);">${t.id}</td>
          <td style="font-weight: 500; color:#fff;">${themeDisplay}</td>
          <td>${statusBadge}</td>
          <td>${beats}</td>
          <td style="font-family: var(--font-mono);">${duration}</td>
          <td>${actionButtons}</td>
        </tr>
      `;
    }).join('');
  }

  // 4. Model Marketplace Filter & Search Engine
  const setupMarketplaceFilters = () => {
    // Keystroke Search
    searchModelInput.addEventListener('input', (e) => {
      activeFilters.search = e.target.value.trim().toLowerCase();
      applyFilters();
    });

    // Helper bind for category lists
    const bindFilterCategory = (elements, filterKey) => {
      elements.forEach(item => {
        item.addEventListener('click', () => {
          elements.forEach(el => el.classList.remove('active'));
          item.classList.add('active');
          activeFilters[filterKey] = item.getAttribute('data-value');
          applyFilters();
        });
      });
    };

    bindFilterCategory(filterProviders, 'provider');
    bindFilterCategory(filterTypes, 'type');
    bindFilterCategory(filterTags, 'tag');
    bindFilterCategory(filterBillings, 'billing');

    // Reset Filters Button
    btnResetAll.addEventListener('click', () => {
      searchModelInput.value = '';
      activeFilters.search = '';
      activeFilters.provider = 'all';
      activeFilters.type = 'all';
      activeFilters.tag = 'all';
      activeFilters.billing = 'all';

      // Reset active list classes
      const resetListClasses = (list) => {
        list.forEach(el => {
          if (el.getAttribute('data-value') === 'all') {
            el.classList.add('active');
          } else {
            el.classList.remove('active');
          }
        });
      };

      resetListClasses(filterProviders);
      resetListClasses(filterTypes);
      resetListClasses(filterTags);
      resetListClasses(filterBillings);

      applyFilters();
    });

    // Model ID Copying inside cards
    document.querySelectorAll('.btn-copy-id').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation(); // Prevent card trigger
        const modelId = btn.getAttribute('data-id');
        navigator.clipboard.writeText(modelId).then(() => {
          const originalSvg = btn.innerHTML;
          btn.innerHTML = `<span style="font-size:10px; font-weight:600; color:var(--success);">✓</span>`;
          btn.style.borderColor = 'var(--success)';
          setTimeout(() => {
            btn.innerHTML = originalSvg;
            btn.style.borderColor = '';
          }, 1200);
        });
      });
    });

    // "立即体验" (Try Now) button action inside cards
    document.querySelectorAll('.btn-try-now').forEach(btn => {
      btn.addEventListener('click', () => {
        const modelId = btn.getAttribute('data-id');
        const modelType = btn.getAttribute('data-type');
        
        // 1. Switch active portal tab to Sandbox Playground
        swapTab('playground');

        // 2. Set Playground endpoint according to type
        if (modelType === 'image') {
          playEndpoint.value = 'image-gen';
        } else if (modelType === 'video') {
          playEndpoint.value = 'video-gen';
        } else {
          playEndpoint.value = 'chat';
        }

        // 3. Trigger template refresh
        updatePlaygroundTemplate();

        // 4. Update model property inside payload JSON editor
        try {
          const payload = JSON.parse(playBody.value);
          payload.model = modelId;
          playBody.value = JSON.stringify(payload, null, 2);
        } catch (e) {}

        // 5. Log connection success to playground console
        if (modelType === 'video') {
          logToPlayConsole(`成功从模型中心载入视频生成模型 [${modelId}] 到接口沙盒。`, 'system');
        } else {
          logToPlayConsole(`成功从模型中心载入模型 [${modelId}] 到接口沙盒中。请点击下方“发送 API 请求”进行实时联调体验。`, 'system');
        }
      });
    });
  };

  // Perform dynamic filtering based on selections
  function applyFilters() {
    let matchCount = 0;
    
    marketCards.forEach(card => {
      const provider = card.getAttribute('data-provider');
      const type = card.getAttribute('data-type');
      const name = card.getAttribute('data-name').toLowerCase();
      const tagsString = card.getAttribute('data-tags') || '';
      const tags = tagsString.split(',');

      // Condition 1: Search text
      const matchesSearch = !activeFilters.search || 
                            name.includes(activeFilters.search) || 
                            provider.toLowerCase().includes(activeFilters.search) ||
                            tagsString.toLowerCase().includes(activeFilters.search);

      // Condition 2: Provider
      const matchesProvider = activeFilters.provider === 'all' || provider === activeFilters.provider;

      // Condition 3: Type
      const matchesType = activeFilters.type === 'all' || type === activeFilters.type;

      // Condition 4: Tags
      const matchesTag = activeFilters.tag === 'all' || tags.includes(activeFilters.tag);

      // Condition 5: Billing (all are payg for now)
      const matchesBilling = activeFilters.billing === 'all' || activeFilters.billing === 'payg';

      if (matchesSearch && matchesProvider && matchesType && matchesTag && matchesBilling) {
        card.style.display = 'flex';
        card.style.opacity = '0';
        setTimeout(() => { card.style.opacity = '1'; }, 20); // Smooth fade transition
        matchCount++;
      } else {
        card.style.display = 'none';
      }
    });

    // Render "No matches" container if grid is empty
    const noResultsId = 'market-no-results';
    let noResultsEl = document.getElementById(noResultsId);
    if (matchCount === 0) {
      if (!noResultsEl) {
        noResultsEl = document.createElement('div');
        noResultsEl.id = noResultsId;
        noResultsEl.style.gridColumn = '1 / -1';
        noResultsEl.style.textAlign = 'center';
        noResultsEl.style.padding = '60px 0';
        noResultsEl.style.color = 'var(--text-muted)';
        noResultsEl.style.fontSize = '14px';
        noResultsEl.textContent = '🔍 没有找到符合当前过滤条件的模型。您可以点击左侧“重置”清空过滤器。';
        marketModelGrid.appendChild(noResultsEl);
      }
    } else {
      if (noResultsEl) {
        noResultsEl.remove();
      }
    }
  }

  // 5. Model Hub Suffix Generator
  const calculateModelSuffix = () => {
    const base = suffixBaseModel.value;
    const ratio = suffixRatio.value;
    const quality = suffixQuality.value;

    let finalModel = base;
    if (base.includes('nano-banana-2')) {
      finalModel = finalModel.replace('nano-banana-2', 'gemini-3.1-flash-image');
    }

    const aspectSuffix = ratio;
    let qualitySuffix = '-2k';
    if (quality === '4k') {
      qualitySuffix = '-4k';
    } else if (quality === '1k') {
      qualitySuffix = ''; 
    }

    const outputId = `${finalModel}-${aspectSuffix}${qualitySuffix}`;
    generatedModelId.textContent = outputId;

    const resolutionMap = {
      '1-1': { '1k': '1024 × 1024', '2k': '2048 × 2048', '4k': '4096 × 4096' },
      '9-16': { '1k': '768 × 1376', '2k': '1536 × 2752', '4k': '3072 × 5504' },
      '16-9': { '1k': '1376 × 768', '2k': '2752 × 1536', '4k': '5504 × 3072' },
      '3-2': { '1k': '1264 × 848', '2k': '2528 × 1696', '4k': '5056 × 3392' },
      '2-3': { '1k': '848 × 1264', '2k': '1696 × 2528', '4k': '3392 × 5056' },
      '21-9': { '1k': '1584 × 672', '2k': '3168 × 1344', '4k': '6336 × 2688' },
    };

    const pixels = resolutionMap[ratio] ? resolutionMap[ratio][quality] : '1024 × 1024';
    generatedModelPixels.textContent = pixels;
  };

  [suffixBaseModel, suffixRatio, suffixQuality].forEach(ctrl => {
    ctrl.addEventListener('change', calculateModelSuffix);
  });

  btnCopyModelId.addEventListener('click', () => {
    navigator.clipboard.writeText(generatedModelId.textContent).then(() => {
      const originalText = btnCopyModelId.textContent;
      btnCopyModelId.textContent = '已成功复制！';
      btnCopyModelId.style.background = 'var(--success)';
      btnCopyModelId.style.color = '#fff';
      setTimeout(() => {
        btnCopyModelId.textContent = originalText;
        btnCopyModelId.style.background = '';
        btnCopyModelId.style.color = '';
      }, 1500);
    });
  });

  // 6. API Documentation & Code Snippets Tabs
  const docMenuItems = document.querySelectorAll('.docs-menu-item');
  const docPanels = document.querySelectorAll('.doc-panel');
  let docSelectedLang = 'curl';

  docMenuItems.forEach(item => {
    item.addEventListener('click', () => {
      docMenuItems.forEach(mi => mi.classList.remove('active'));
      item.classList.add('active');

      const docId = item.getAttribute('data-doc');
      docPanels.forEach(p => p.style.display = 'none');
      const activePanel = document.getElementById(`doc-${docId}`);
      if (activePanel) {
        activePanel.style.display = 'block';
      }
      renderDocSnippets();
    });
  });

  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('code-tab-btn')) {
      const header = e.target.parentElement;
      const lang = e.target.getAttribute('data-lang');
      docSelectedLang = lang;

      header.querySelectorAll('.code-tab-btn').forEach(btn => btn.classList.remove('active'));
      e.target.classList.add('active');

      renderDocSnippets();
    }
  });

  // Syntax Highlighting Engine for cURL, Python, and JS
  function highlightCode(code, lang) {
    // Escape HTML
    let html = code
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    if (lang === 'curl') {
      html = html
        .replace(/\b(curl)\b/g, '<span class="token-keyword">$1</span>')
        .replace(/(\s-[A-Za-z]\b|\s--[A-Za-z-]+\b)/g, '<span class="token-method">$1</span>')
        .replace(/(Authorization:|Content-Type:|Bearer)/g, '<span class="token-comment">$1</span>')
        .replace(/("application\/json"|"multipart\/form-data")/g, '<span class="token-string">$1</span>')
        .replace(/(https?:\/\/[^\s\"\\]+)/g, '<span class="token-url">$1</span>')
        .replace(/(YOUR_[A-Z_]+)/g, '<span class="token-string">$1</span>');
    } else if (lang === 'python') {
      html = html
        .replace(/\b(import|from|print|with|as)\b/g, '<span class="token-keyword">$1</span>')
        .replace(/(#.*)$/gm, '<span class="token-comment">$1</span>')
        .replace(/(\".*?\"|\'.*?\')/g, '<span class="token-string">$1</span>')
        .replace(/(https?:\/\/[^\s\"\']+)/g, '<span class="token-url">$1</span>')
        .replace(/(YOUR_[A-Z_]+)/g, '<span class="token-string">$1</span>');
    } else if (lang === 'js' || lang === 'javascript') {
      html = html
        .replace(/\b(const|let|var|function|return|then|catch|console|log|method|headers|body|JSON|stringify|fetch)\b/g, '<span class="token-keyword">$1</span>')
        .replace(/(\/\/.*)$/gm, '<span class="token-comment">$1</span>')
        .replace(/(\".*?\"|\'.*?\'|\`.*?\`)/g, '<span class="token-string">$1</span>')
        .replace(/(https?:\/\/[^\s\"\`\']+)/g, '<span class="token-url">$1</span>')
        .replace(/(YOUR_[A-Z_]+)/g, '<span class="token-string">$1</span>');
    }
    return html;
  }

  function renderDocSnippets() {
    const activePanel = document.querySelector('.doc-panel:not([style*="display: none"])');
    if (!activePanel) return;

    const endpointId = activePanel.id.replace('doc-', '');
    const preBlock = activePanel.querySelector('.code-block');
    if (!preBlock) return;

    const currentHost = window.location.origin;
    const tunnelOrigin = serverManaged ? currentHost : 'https://your-tunnel.trycloudflare.com';
    const keyString = serverManaged ? 'YOUR_ACCESS_CODE' : 'YOUR_API_KEY';

    let code = '';
    
    if (endpointId === 'post-chat') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyString}" \\
  -d '{
    "model": "gemini-3-flash-agent",
    "messages": [
      {"role": "system", "content": "You are a creative assistant."},
      {"role": "user", "content": "设计一个废弃巴士的改造点子"}
    ],
    "temperature": 0.7
  }'`;
      } else if (docSelectedLang === 'python') {
        code = `from openai import OpenAI

client = OpenAI(
    base_url="${tunnelOrigin}/v1",
    api_key="${keyString}"
)

response = client.chat.completions.create(
    model="gemini-3-flash-agent",
    messages=[
        {"role": "user", "content": "设计一个废弃巴士的改造点子"}
    ]
)
print(response.choices[0].message.content)`;
      } else {
        code = `fetch("${tunnelOrigin}/v1/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${keyString}"
  },
  body: JSON.stringify({
    model: "gemini-3-flash-agent",
    messages: [{"role": "user", "content": "设计一个废弃巴士的改造点子"}]
  })
})
.then(res => res.json())
.then(data => console.log(data.choices[0].message.content));`;
      }
    } 
    
    else if (endpointId === 'post-image-gen') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/v1/images/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyString}" \\
  -d '{
    "model": "nano-banana-2",
    "prompt": "清晨薄雾中的现代森林住宅，真实摄影",
    "size": "9:16",
    "quality": "2K",
    "response_format": "b64_json"
  }'`;
      } else if (docSelectedLang === 'python') {
        code = `import base64
import requests

response = requests.post(
    "${tunnelOrigin}/v1/images/generations",
    headers={"Authorization": "Bearer ${keyString}"},
    json={
        "model": "nano-banana-2",
        "prompt": "清晨薄雾中的现代森林住宅，真实摄影",
        "size": "9:16",
        "quality": "2K",
        "response_format": "b64_json"
    }
)
image_data = response.json()["data"][0]["b64_json"]
with open("output.png", "wb") as f:
    f.write(base64.b64decode(image_data))`;
      } else {
        code = `fetch("${tunnelOrigin}/v1/images/generations", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${keyString}"
  },
  body: JSON.stringify({
    model: "nano-banana-2",
    prompt: "清晨薄雾中的现代森林住宅，真实摄影",
    size: "9:16",
    quality: "2K",
    response_format": "b64_json"
  })
})
.then(res => res.json())
.then(data => {
  const imgBase64 = data.data[0].b64_json;
  console.log("得到图像 Base64 长度:", imgBase64.length);
});`;
      }
    } 
    
    else if (endpointId === 'post-ideate') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/api/ideate \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyString}" \\
  -d '{
    "count": 8
  }'`;
      } else if (docSelectedLang === 'python') {
        code = `import requests

response = requests.post(
    "${tunnelOrigin}/api/ideate",
    headers={"Authorization": "Bearer ${keyString}"},
    json={"count": 8}
)
print("策划创意列表:", response.json()["ideas"])`;
      } else {
        code = `fetch("${tunnelOrigin}/api/ideate", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${keyString}"
  },
  body: JSON.stringify({ count: 8 })
})
.then(res => res.json())
.then(data => console.log("策划创意:", data.ideas));`;
      }
    } 
    
    else if (endpointId === 'post-compose') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/api/compose \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${keyString}" \\
  -d '{
    "dimensions": {
      "theme": "林间校车树屋",
      "anchors": ["废弃轮廓", "植物侵蚀", "复古改造"],
      "complexity": "高",
      "budget": "中等",
      "ratio": "80",
      "creativity": "极高",
      "beats_count": 15
    }
  }'`;
      } else if (docSelectedLang === 'python') {
        code = `import requests

response = requests.post(
    "${tunnelOrigin}/api/compose",
    headers={"Authorization": "Bearer ${keyString}"},
    json={
        "dimensions": {
            "theme": "林间校车树屋",
            "anchors": ["废弃轮廓", "植物侵蚀", "复古改造"],
            "complexity": "高",
            "budget": "中等",
            "ratio": "80",
            "creativity": "极高",
            "beats_count": 15
        }
    }
)
task_id = response.json()["task_id"]
print("时光机异步任务启动成功，任务ID:", task_id)`;
      } else {
        code = `fetch("${tunnelOrigin}/api/compose", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${keyString}"
  },
  body: JSON.stringify({
    dimensions: {
      theme: "林间校车树屋",
      anchors: ["废弃轮廓", "植物侵蚀", "复古改造"],
      complexity: "高",
      budget: "中等",
      ratio: "80",
      creativity: "极高",
      beats_count: 15
    }
  })
})
.then(res => res.json())
.then(data => console.log("合成任务已启动，ID:", data.task_id));`;
      }
    } 
    
    else if (endpointId === 'post-image-edit') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/api/image/edits \\
  -H "Authorization: Bearer ${keyString}" \\
  -F "image=@/path/to/source.png" \\
  -F "prompt=将画面光线改为清晨，保持构图和后院餐桌桌椅不变" \\
  -F "model=nano-banana-2" \\
  -F "aspect_ratio=9:16" \\
  -F "image_size=2K" \\
  -F "response_format=b64_json"`;
      } else if (docSelectedLang === 'python') {
        code = `import requests
import base64

with open("source.png", "rb") as ref:
    response = requests.post(
        "${tunnelOrigin}/api/image/edits",
        headers={"Authorization": "Bearer ${keyString}"},
        files={"image": ("source.png", ref, "image/png")},
        data={
            "model": "nano-banana-2",
            "prompt": "将画面光线改为清晨，保持构图和后院餐桌桌椅不变",
            "aspect_ratio": "9:16",
            "image_size": "2K",
            "response_format": "b64_json"
        }
    )
b64_data = response.json()["data"][0]["b64_json"]
with open("edited_output.png", "wb") as f:
    f.write(base64.b64decode(b64_data))`;
      } else {
        code = `const formData = new FormData();
const imageFile = document.getElementById("file-input").files[0];

formData.append("image", imageFile);
formData.append("prompt", "将画面光线改为清晨，保持构图和后院餐桌桌椅不变");
formData.append("model", "nano-banana-2");
formData.append("aspect_ratio", "9:16");
formData.append("image_size", "2K");
formData.append("response_format", "b64_json");

fetch("${tunnelOrigin}/api/image/edits", {
  method: "POST",
  headers: {
    "Authorization": "Bearer ${keyString}"
  },
  body: formData
})
.then(res => res.json())
.then(data => console.log("编辑图像生成成功！"));`;
      }
    } 
    
    else if (endpointId === 'post-reverse-video') {
      if (docSelectedLang === 'curl') {
        code = `curl ${tunnelOrigin}/api/reverse-video \\
  -H "Authorization: Bearer ${keyString}" \\
  -F "video=@/path/to/lapse.mp4" \\
  -F "fps=3.0"`;
      } else if (docSelectedLang === 'python') {
        code = `import requests

with open("lapse.mp4", "rb") as video_file:
    response = requests.post(
        "${tunnelOrigin}/api/reverse-video",
        headers={"Authorization": "Bearer ${keyString}"},
        files={"video": ("lapse.mp4", video_file, "video/mp4")},
        data={"fps": 3.0}
    )
print("解析出的时序关键帧语义与提示词序列:", response.json())`;
      } else {
        code = `const formData = new FormData();
const videoFile = document.getElementById("video-input").files[0];

formData.append("video", videoFile);
formData.append("fps", 3.0);

fetch("${tunnelOrigin}/api/reverse-video", {
  method: "POST",
  headers: {
    "Authorization": "Bearer ${keyString}"
  },
  body: formData
})
.then(res => res.json())
.then(data => console.log("视频时序关键帧解析成功：", data));`;
      }
    }

    preBlock.innerHTML = highlightCode(code, docSelectedLang);
  }

  window.copyDocCode = (blockId) => {
    const preBlock = document.getElementById(blockId);
    if (!preBlock) return;

    navigator.clipboard.writeText(preBlock.textContent).then(() => {
      const copyBtn = preBlock.parentElement.querySelector('.btn-copy-code');
      const originalSvg = copyBtn.innerHTML;
      copyBtn.innerHTML = `<span style="font-size:10px; font-weight:600; color:var(--success);">✓</span>`;
      setTimeout(() => {
        copyBtn.innerHTML = originalSvg;
      }, 1500);
    });
  };

  // 7. Interactive Playground Sandbox
  const updatePlaygroundTemplate = () => {
    const endpoint = playEndpoint.value;
    const devToken = localTokenInput.value.trim();
    const mockConfig = {
      baseUrl: 'http://127.0.0.1:8046/v1',
      apiKey: serverManaged ? '' : (devToken || 'YOUR_API_KEY')
    };

    let template = {};

    if (endpoint === 'ideate') {
      playFilesGroup.style.display = 'none';
      template = {
        count: 8,
        config: mockConfig
      };
    } 
    
    else if (endpoint === 'image-gen') {
      playFilesGroup.style.display = 'none';
      template = {
        model: 'nano-banana-2',
        prompt: '清晨薄雾中的现代森林住宅，真实摄影',
        size: '9:16',
        quality: '2K',
        response_format: 'b64_json',
        config: mockConfig
      };
    } 
    
    else if (endpoint === 'image-edit') {
      playFilesGroup.style.display = 'block';
      template = {
        model: 'nano-banana-2',
        prompt: '将画面光线改为清晨，保持参考图的所有内容、结构、人物与相机角度不变。',
        aspect_ratio: '9:16',
        image_size: '2K',
        response_format: 'b64_json'
      };
    } 
    
    else if (endpoint === 'chat') {
      template = {
        model: 'gpt-4o-mini',
        messages: [
          { role: 'system', content: 'You are a creative renovations design assistant.' },
          { role: 'user', content: '为一辆旧校车的装修改造方案起3个富有想象力的名字，并简短描述其概念风格。' }
        ],
        temperature: 0.7,
        config: mockConfig
      };
    }
    
    else if (endpoint === 'video-gen') {
      template = {
        model: 'doubao-seedance-2-0-260128',
        content: [
          {
            type: 'text',
            text: '在<图片1> 场景内，为旋转的产品制作炫酷的cg展示动画，镜头从产品背面开始环绕运镜。'
          }
        ],
        ratio: '16:9',
        resolution: '720p',
        duration: 8,
        generate_audio: false,
        return_last_frame: false,
        watermark: false,
        config: mockConfig
      };
    }

    // Dynamic visibility and helper text for premium visual uploader
    const uploaderTextEl = playFilesGroup.querySelector('.uploader-text');
    const uploaderSubEl = playFilesGroup.querySelector('.uploader-sub');
    
    if (endpoint === 'image-edit') {
      playFilesGroup.style.display = 'block';
      if (uploaderTextEl) uploaderTextEl.textContent = '选择或拖拽图生图参考图片';
      if (uploaderSubEl) uploaderSubEl.textContent = '第一张图为重绘 Canvas (主图)，后续图片为材质/风格参考';
    } else if (endpoint === 'chat') {
      playFilesGroup.style.display = 'block';
      if (uploaderTextEl) uploaderTextEl.textContent = '选择或拖拽视觉对话参考图片 (多选)';
      if (uploaderSubEl) uploaderSubEl.textContent = '支持 GPT-4o, GPT-4o-mini 和 Gemini 多模态识图对话';
    } else if (endpoint === 'video-gen') {
      playFilesGroup.style.display = 'block';
      if (uploaderTextEl) uploaderTextEl.textContent = '选择或拖拽视频生成主体/场景参考图';
      if (uploaderSubEl) uploaderSubEl.textContent = '作为 Doubao Seedance 2.0 视频生成的首帧或主体输入';
    } else {
      playFilesGroup.style.display = 'none';
    }

    playBody.value = JSON.stringify(template, null, 2);
  };

  playEndpoint.addEventListener('change', updatePlaygroundTemplate);

  const logToPlayConsole = (msg, type = 'info') => {
    const now = new Date().toLocaleTimeString();
    
    // Remove existing cursor if any
    const oldCursor = playConsoleLog.querySelector('.console-cursor');
    if (oldCursor) oldCursor.remove();

    const line = document.createElement('div');
    line.className = 'console-log-line';

    let prefixClass = 'prefix-info';
    let prefixText = '⚙️ [SYSTEM]';
    if (type === 'request') {
      prefixClass = 'prefix-info';
      prefixText = '⚡ [REQUEST]';
    } else if (type === 'response') {
      prefixClass = 'prefix-success';
      prefixText = '📥 [RESPONSE]';
    } else if (type === 'error') {
      prefixClass = 'prefix-error';
      prefixText = '❌ [ERROR]';
    } else if (type === 'system') {
      prefixClass = 'prefix-system';
      prefixText = '⚙️ [SYSTEM]';
    }

    line.innerHTML = `
      <span class="console-timestamp" style="color:var(--text-dark); font-size:11px; margin-right:6px;">[${now}]</span>
      <span class="console-prefix ${prefixClass}">${prefixText}</span>
      <span class="console-message" style="color:var(--text-main);">${msg}</span>
    `;
    
    playConsoleLog.appendChild(line);

    // Re-append blinking cursor at the end
    const cursor = document.createElement('span');
    cursor.className = 'console-cursor';
    playConsoleLog.appendChild(cursor);

    playConsoleLog.scrollTop = playConsoleLog.scrollHeight;
  };

  btnClearConsole.addEventListener('click', () => {
    playConsoleLog.innerHTML = '';
    logToPlayConsole('控制台日志已清空。', 'system');
  });

  btnSendRequest.addEventListener('click', async () => {
    const endpoint = playEndpoint.value;
    let payload = {};
    
    if (videoPollInterval) {
      clearInterval(videoPollInterval);
      videoPollInterval = null;
    }
    
    try {
      payload = JSON.parse(playBody.value);
    } catch (err) {
      logToPlayConsole(`JSON 解析错误：${err.message}`, 'error');
      alert('请求 Payload 的 JSON 格式不正确，请修改后重试！');
      return;
    }

    btnSendRequest.disabled = true;
    btnSendRequest.querySelector('span').textContent = '请求发送中...';
    playPreviewPanel.style.display = 'none';

    let targetUrl = '';
    let method = 'POST';
    let headers = { 'Accept': 'application/json' };
    let bodyData = null;

    const devToken = localTokenInput.value.trim();
    if (needsAccessCode) {
      headers['X-Access-Code'] = devToken;
      headers['Authorization'] = `Bearer ${devToken}`;
    } else if (devToken && !serverManaged) {
      headers['Authorization'] = `Bearer ${devToken}`;
    }

    logToPlayConsole(`准备向 ${endpoint} 接口发送请求...`, 'system');

    if (endpoint === 'ideate') {
      targetUrl = '/api/ideate';
      headers['Content-Type'] = 'application/json';
      bodyData = JSON.stringify(payload);
    } 
    
    else if (endpoint === 'image-gen') {
      targetUrl = '/api/image/generations';
      headers['Content-Type'] = 'application/json';
      bodyData = JSON.stringify(payload);
    } 
    
    else if (endpoint === 'chat') {
      targetUrl = '/v1/chat/completions';
      headers['Content-Type'] = 'application/json';
      
      // Automatically construct multimodal payload if files are visually uploaded
      if (uploadedFiles.length > 0) {
        logToPlayConsole(`检测到 ${uploadedFiles.length} 张参考图，正在转换 Base64 并构建多模态对话结构...`, 'system');
        try {
          const imageParts = [];
          for (let i = 0; i < uploadedFiles.length; i++) {
            const dataUrl = await readFileAsDataUrl(uploadedFiles[i]);
            imageParts.push({
              type: 'image_url',
              image_url: {
                url: dataUrl
              }
            });
          }
          
          if (!payload.messages || payload.messages.length === 0) {
            payload.messages = [{ role: 'user', content: '' }];
          }
          
          // Find the last user message to attach the images
          let lastUserMsg = null;
          for (let i = payload.messages.length - 1; i >= 0; i--) {
            if (payload.messages[i].role === 'user') {
              lastUserMsg = payload.messages[i];
              break;
            }
          }
          if (!lastUserMsg) {
            lastUserMsg = { role: 'user', content: '' };
            payload.messages.push(lastUserMsg);
          }
          
          const textVal = typeof lastUserMsg.content === 'string' ? lastUserMsg.content : '';
          lastUserMsg.content = [
            { type: 'text', text: textVal }
          ].concat(imageParts);
          
          logToPlayConsole(`已成功将 ${uploadedFiles.length} 张图片并入 messages[${payload.messages.indexOf(lastUserMsg)}].content 中。`, 'system');
        } catch (e) {
          logToPlayConsole(`多模态图片 Base64 转换失败: ${e.message}`, 'error');
          alert('图片转换失败，请重试！');
          btnSendRequest.disabled = false;
          btnSendRequest.querySelector('span').textContent = '发送 API 请求';
          return;
        }
      }
      
      bodyData = JSON.stringify(payload);
    } 
    
    else if (endpoint === 'video-gen') {
      targetUrl = '/api/contents/generations/tasks';
      headers['Content-Type'] = 'application/json';
      
      // Inject visually uploaded reference image into the video task content
      if (uploadedFiles.length > 0) {
        logToPlayConsole(`检测到视频生成主体参考图，正在注入 content 字段...`, 'system');
        try {
          const dataUrl = await readFileAsDataUrl(uploadedFiles[0]);
          if (!Array.isArray(payload.content)) {
            payload.content = [];
          }
          
          const refImgIdx = payload.content.findIndex(item => item.role === 'reference_image');
          if (refImgIdx !== -1) {
            payload.content[refImgIdx].image_url = { url: dataUrl };
          } else {
            payload.content.push({
              type: 'image_url',
              role: 'reference_image',
              image_url: {
                url: dataUrl
              }
            });
          }
          logToPlayConsole(`成功并入视频生成参考图 Base64 首帧。`, 'system');
        } catch (e) {
          logToPlayConsole(`视频参考图转换失败: ${e.message}`, 'error');
        }
      }
      
      bodyData = JSON.stringify(payload);
    }
    
    else if (endpoint === 'image-edit') {
      targetUrl = '/api/image/edits';
      const formData = new FormData();
      
      if (uploadedFiles.length === 0) {
        logToPlayConsole('错误: 图生图接口必须上传主参考图 (image)!', 'error');
        alert('请先选择并上传参考图！');
        btnSendRequest.disabled = false;
        btnSendRequest.querySelector('span').textContent = '发送 API 请求';
        return;
      }
      
      formData.append('image', uploadedFiles[0]);
      
      for (let i = 1; i < uploadedFiles.length; i++) {
        formData.append('image[]', uploadedFiles[i]);
      }

      formData.append('prompt', payload.prompt || '');
      formData.append('model', payload.model || 'nano-banana-2');
      formData.append('aspect_ratio', payload.aspect_ratio || '9:16');
      formData.append('image_size', payload.image_size || '2K');
      formData.append('response_format', payload.response_format || 'b64_json');
      
      const devToken = localTokenInput.value.trim();
      formData.append('config', JSON.stringify({
        baseUrl: 'http://127.0.0.1:8046/v1',
        apiKey: serverManaged ? '' : devToken
      }));

      bodyData = formData;
    }

    logToPlayConsole(`正在发出 POST 请求: ${targetUrl}`, 'request');

    try {
      const response = await fetch(targetUrl, {
        method: method,
        headers: headers,
        body: bodyData
      });

      logToPlayConsole(`收到响应，HTTP 状态码: ${response.status}`, 'response');
      
      const responseText = await response.text();
      let resJson = null;
      try {
        resJson = JSON.parse(responseText);
      } catch (e) {
        logToPlayConsole(`响应不是 JSON 格式:\n${responseText.slice(0, 1000)}`, 'response');
      }

      if (resJson) {
        logToPlayConsole(JSON.stringify(resJson, null, 2), 'response');

        // Check if it's a video task creation success
        if (endpoint === 'video-gen' && resJson.id) {
          const taskId = resJson.id;
          logToPlayConsole(`视频生成任务创建成功！任务 ID: ${taskId}。开始轮询任务状态...`, 'system');
          
          playPreviewPanel.style.display = 'none';
          const playPreviewVideo = document.getElementById('play-preview-video');
          const playPreviewTitle = document.getElementById('play-preview-title');
          
          btnSendRequest.disabled = true;
          btnSendRequest.querySelector('span').textContent = '视频生成中 (轮询中)...';

          videoPollInterval = setInterval(async () => {
            try {
              let pollHeaders = {};
              if (needsAccessCode) {
                pollHeaders['X-Access-Code'] = devToken;
                pollHeaders['Authorization'] = `Bearer ${devToken}`;
              } else if (devToken && !serverManaged) {
                pollHeaders['Authorization'] = `Bearer ${devToken}`;
              }

              const pollResp = await fetch(`/api/contents/generations/tasks/${taskId}`, {
                headers: pollHeaders
              });
              
              if (!pollResp.ok) {
                throw new Error(`HTTP ${pollResp.status}`);
              }
              
              const pollData = await pollResp.json();
              const status = pollData.status;
              
              logToPlayConsole(`任务 ${taskId} 状态: ${status}`, 'system');
              
              if (status === 'succeeded' || status === 'success') {
                clearInterval(videoPollInterval);
                videoPollInterval = null;
                
                logToPlayConsole(`视频生成任务完成！完整响应 JSON：\n${JSON.stringify(pollData, null, 2)}`, 'response');
                
                const videoUrl = pollData.content && pollData.content.video_url;
                if (videoUrl) {
                  if (playPreviewTitle) playPreviewTitle.textContent = '生成的视频预览';
                  playPreviewImg.style.display = 'none';
                  if (playPreviewVideo) {
                    playPreviewVideo.src = videoUrl;
                    playPreviewVideo.style.display = 'block';
                  }
                  playPreviewPanel.style.display = 'block';
                  
                  btnDownloadImg.textContent = '下载该视频';
                  btnDownloadImg.onclick = () => {
                    const link = document.createElement('a');
                    link.href = videoUrl;
                    link.download = `spark_video_generated_${Date.now()}.mp4`;
                    document.body.appendChild(link);
                    link.click();
                    document.body.removeChild(link);
                  };
                  
                  logToPlayConsole('检测到生成的视频数据，已在下方预览面板渲染播放。', 'system');
                } else {
                  logToPlayConsole('错误：任务成功，但未返回视频 URL。', 'error');
                }
                
                btnSendRequest.disabled = false;
                btnSendRequest.querySelector('span').textContent = '发送 API 请求';
                
              } else if (status === 'failed' || status === 'cancelled' || status === 'not_found' || !status) {
                clearInterval(videoPollInterval);
                videoPollInterval = null;
                
                logToPlayConsole(`视频生成任务结束（状态：${status || '未知'}）！完整响应 JSON：\n${JSON.stringify(pollData, null, 2)}`, 'error');
                
                btnSendRequest.disabled = false;
                btnSendRequest.querySelector('span').textContent = '发送 API 请求';
              }
            } catch (err) {
              logToPlayConsole(`轮询出现异常：${err.message}`, 'error');
            }
          }, 3000);
          
          return; // Skip normal image/chat handling
        }

        let b64Image = null;
        
        if (resJson.data && resJson.data.length > 0 && resJson.data[0].b64_json) {
          b64Image = resJson.data[0].b64_json;
        } else if (resJson.choices && resJson.choices.length > 0 && resJson.choices[0].message.content) {
          const content = resJson.choices[0].message.content;
          const match = content.match(/data:image\/[^;]+;base64,([A-Za-z0-9+/=]+)/);
          if (match) {
            b64Image = match[1];
          }
        }

        if (b64Image) {
          const srcData = b64Image.startsWith('data:image') ? b64Image : `data:image/png;base64,${b64Image}`;
          const playPreviewVideo = document.getElementById('play-preview-video');
          const playPreviewTitle = document.getElementById('play-preview-title');
          
          if (playPreviewTitle) playPreviewTitle.textContent = '生成的图片预览';
          if (playPreviewVideo) playPreviewVideo.style.display = 'none';
          playPreviewImg.src = srcData;
          playPreviewImg.style.display = 'block';
          playPreviewPanel.style.display = 'block';
          btnDownloadImg.textContent = '下载该图片';
          
          logToPlayConsole('检测到生成的图像数据，已在下方预览面板渲染。', 'system');

          btnDownloadImg.onclick = () => {
            const link = document.createElement('a');
            link.href = srcData;
            link.download = `spark_api_generated_${Date.now()}.png`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
          };
        }
      }

      if (!response.ok) {
        logToPlayConsole(`请求失败！错误信息：${responseText.slice(0, 500)}`, 'error');
      }

    } catch (error) {
      logToPlayConsole(`请求发送异常：${error.message}`, 'error');
    } finally {
      // If we are polling video-gen, do not reset the button yet!
      if (endpoint !== 'video-gen') {
        btnSendRequest.disabled = false;
        btnSendRequest.querySelector('span').textContent = '发送 API 请求';
      }
    }
  });

  // Start initialization
  init();
});

// Helper functions for premium visual uploader
const readFileAsDataUrl = (file) => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = (error) => reject(error);
    reader.readAsDataURL(file);
  });
};

let uploadedFiles = [];

const setupVisualUploader = () => {
  const imageUploadZone = document.getElementById('image-upload-zone');
  const sandboxImageInput = document.getElementById('sandbox-image-input');
  
  if (!imageUploadZone || !sandboxImageInput) return;

  // Trigger file input click
  imageUploadZone.addEventListener('click', () => {
    sandboxImageInput.click();
  });

  // Drag and drop event handlers
  imageUploadZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    imageUploadZone.classList.add('dragover');
  });

  imageUploadZone.addEventListener('dragleave', () => {
    imageUploadZone.classList.remove('dragover');
  });

  imageUploadZone.addEventListener('drop', (e) => {
    e.preventDefault();
    imageUploadZone.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) {
      addUploadedFiles(e.dataTransfer.files);
    }
  });

  // File input change handler
  sandboxImageInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
      addUploadedFiles(e.target.files);
    }
  });
};

const addUploadedFiles = (fileList) => {
  for (let i = 0; i < fileList.length; i++) {
    const file = fileList[i];
    if (file.type.startsWith('image/')) {
      if (!uploadedFiles.some(f => f.name === file.name && f.size === file.size)) {
        uploadedFiles.push(file);
      }
    } else {
      alert(`文件 [${file.name}] 不是有效的图片，已被过滤。`);
    }
  }
  renderUploadPreviews();
};

const removeUploadedFile = (index) => {
  uploadedFiles.splice(index, 1);
  renderUploadPreviews();
};

const renderUploadPreviews = () => {
  const sandboxPreviewList = document.getElementById('sandbox-preview-list');
  if (!sandboxPreviewList) return;

  if (uploadedFiles.length === 0) {
    sandboxPreviewList.style.display = 'none';
    sandboxPreviewList.innerHTML = '';
    return;
  }

  sandboxPreviewList.style.display = 'flex';
  sandboxPreviewList.innerHTML = '';

  uploadedFiles.forEach((file, index) => {
    const isPrimary = index === 0;
    
    const card = document.createElement('div');
    card.className = `preview-card ${isPrimary ? 'primary-card' : 'extra-card'}`;
    
    const img = document.createElement('img');
    img.alt = file.name;
    
    const objectUrl = URL.createObjectURL(file);
    img.src = objectUrl;
    img.onload = () => URL.revokeObjectURL(objectUrl);

    const label = document.createElement('div');
    label.className = 'card-label';
    label.textContent = isPrimary ? '主图' : '参考';

    const removeBtn = document.createElement('div');
    removeBtn.className = 'btn-remove';
    removeBtn.textContent = '×';
    removeBtn.title = '删除图片';
    removeBtn.addEventListener('click', (e) => {
      e.stopPropagation(); // prevent click event bubble to parent
      removeUploadedFile(index);
    });

    card.appendChild(img);
    card.appendChild(label);
    card.appendChild(removeBtn);
    sandboxPreviewList.appendChild(card);
  });
};

/* ============================================================
   General Settings Theme Switch (Light/Dark Mode)
   ============================================================ */
(function initThemeToggle() {
  const btn = document.getElementById('theme-toggle-btn');
  const icon = document.getElementById('theme-toggle-icon');
  if (!btn) return;
  const isDark = () => document.documentElement.getAttribute('data-theme') === 'dark';
  const sync = () => {
    if (icon) icon.textContent = isDark() ? '☀️' : '🌙';
    btn.title = isDark() ? '切换到明亮模式' : '切换到暗夜模式';
  };
  sync();
  btn.addEventListener('click', () => {
    const next = !isDark();
    document.documentElement.setAttribute('data-theme', next ? 'dark' : 'light');
    try { localStorage.setItem('spark_theme', next ? 'dark' : 'light'); } catch (e) {}
    sync();
  });
})();

/* ============================================================
   Mobile Sidebar Toggle & Filter Panel Drawer Control
   ============================================================ */
(function initMobileDrawers() {
  // Elements
  const mobileMenuToggle = document.getElementById('mobile-menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  const sidebarBackdrop = document.getElementById('sidebar-backdrop');
  
  const mobileFilterToggle = document.getElementById('mobile-filter-toggle');
  const marketSidebar = document.querySelector('.market-sidebar');
  const btnCloseFilters = document.getElementById('btn-close-filters');

  if (!sidebarBackdrop) return;

  // Function to close all mobile drawers
  const closeAllDrawers = () => {
    if (sidebar) sidebar.classList.remove('open');
    if (marketSidebar) marketSidebar.classList.remove('open');
    sidebarBackdrop.classList.remove('active');
  };

  // Toggle Sidebar
  if (mobileMenuToggle && sidebar) {
    mobileMenuToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      sidebar.classList.add('open');
      sidebarBackdrop.classList.add('active');
    });
  }

  // Toggle Model Filter Drawer
  if (mobileFilterToggle && marketSidebar) {
    mobileFilterToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      marketSidebar.classList.add('open');
      sidebarBackdrop.classList.add('active');
    });
  }

  // Close Filter Drawer via Close Button
  if (btnCloseFilters && marketSidebar) {
    btnCloseFilters.addEventListener('click', (e) => {
      e.stopPropagation();
      closeAllDrawers();
    });
  }

  // Dismiss Drawers via Backdrop click
  sidebarBackdrop.addEventListener('click', () => {
    closeAllDrawers();
  });

  // Auto-close Sidebar Drawer when clicking navigation items on mobile screens
  const navLinks = document.querySelectorAll('.sidebar .nav-item, .sidebar .nav-item-link');
  navLinks.forEach(link => {
    link.addEventListener('click', () => {
      if (window.innerWidth <= 1024) {
        closeAllDrawers();
      }
    });
  });
})();

// --- LIGHTBOX SYSTEM FOR CONSOLE ---
(function() {
    let lightboxItems = [];
    let lightboxActiveIndex = -1;

    function initLightbox() {
        const modal = document.getElementById('lightbox-modal');
        const closeBtn = document.getElementById('close-lightbox-btn');
        const prevBtn = document.getElementById('prev-lightbox-btn');
        const nextBtn = document.getElementById('next-lightbox-btn');
        
        if (!modal) return;
        
        closeBtn?.addEventListener('click', closeLightbox);
        prevBtn?.addEventListener('click', () => navigateLightbox(-1));
        nextBtn?.addEventListener('click', () => navigateLightbox(1));
        
        // Close on clicking outside the content
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                closeLightbox();
            }
        });

        // Add click listeners to the preview elements
        const previewImg = document.getElementById('play-preview-img');
        const previewVideo = document.getElementById('play-preview-video');

        if (previewImg) {
            previewImg.style.cursor = 'pointer';
            previewImg.addEventListener('click', () => {
                if (previewImg.src) {
                    openLightbox([{
                        type: 'image',
                        url: previewImg.src,
                        caption: '<strong>API 生成图片预览 (API Generated Image)</strong>'
                    }], 0);
                }
            });
        }

        if (previewVideo) {
            // Remove controls in the inline video so clicking it doesn't toggle native play/pause in conflict with lightbox
            // Wait, we can keep controls but add a click listener. Let's make it click-to-preview.
            previewVideo.style.cursor = 'pointer';
            previewVideo.addEventListener('click', (e) => {
                // If clicked on the video itself (not controls), open lightbox
                // On some browsers, clicking the video element with controls triggers the click event.
                // We can open the lightbox.
                if (previewVideo.src) {
                    openLightbox([{
                        type: 'video',
                        url: previewVideo.src,
                        caption: '<strong>API 生成视频预览 (API Generated Video)</strong>'
                    }], 0);
                }
            });
        }
    }

    function openLightbox(items, index) {
        lightboxItems = items;
        lightboxActiveIndex = index;
        
        const modal = document.getElementById('lightbox-modal');
        if (!modal) return;
        
        modal.style.display = 'flex';
        updateLightboxContent();
        
        document.addEventListener('keydown', handleLightboxKeydown);
    }

    function closeLightbox() {
        const modal = document.getElementById('lightbox-modal');
        if (!modal) return;
        modal.style.display = 'none';
        
        const video = document.getElementById('lightbox-video');
        if (video) {
            video.pause();
            video.src = '';
        }
        
        document.removeEventListener('keydown', handleLightboxKeydown);
    }

    function updateLightboxContent() {
        const img = document.getElementById('lightbox-img');
        const video = document.getElementById('lightbox-video');
        const caption = document.getElementById('lightbox-caption');
        const prevBtn = document.getElementById('prev-lightbox-btn');
        const nextBtn = document.getElementById('next-lightbox-btn');
        
        if (!img || !video || !caption) return;
        
        if (lightboxActiveIndex < 0 || lightboxActiveIndex >= lightboxItems.length) {
            closeLightbox();
            return;
        }
        
        const item = lightboxItems[lightboxActiveIndex];
        
        if (item.type === 'video') {
            img.style.display = 'none';
            video.src = item.url;
            video.style.display = 'block';
            video.play().catch(err => console.log("Auto-play prevented", err));
        } else {
            video.style.display = 'none';
            video.pause();
            video.src = '';
            img.src = item.url;
            img.style.display = 'block';
        }
        
        if (item.caption) {
            caption.innerHTML = item.caption;
            caption.style.display = 'block';
        } else {
            caption.style.display = 'none';
        }
        
        if (lightboxItems.length <= 1) {
            if (prevBtn) prevBtn.style.display = 'none';
            if (nextBtn) nextBtn.style.display = 'none';
        } else {
            if (prevBtn) prevBtn.style.display = 'flex';
            if (nextBtn) nextBtn.style.display = 'flex';
        }
    }

    function navigateLightbox(direction) {
        if (lightboxItems.length <= 1) return;
        
        lightboxActiveIndex += direction;
        if (lightboxActiveIndex < 0) {
            lightboxActiveIndex = lightboxItems.length - 1;
        } else if (lightboxActiveIndex >= lightboxItems.length) {
            lightboxActiveIndex = 0;
        }
        
        updateLightboxContent();
    }

    function handleLightboxKeydown(e) {
        if (e.key === 'ArrowLeft') {
            navigateLightbox(-1);
        } else if (e.key === 'ArrowRight') {
            navigateLightbox(1);
        } else if (e.key === 'Escape') {
            closeLightbox();
        }
    }

    // Initialize on load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initLightbox);
    } else {
        initLightbox();
    }
})();

