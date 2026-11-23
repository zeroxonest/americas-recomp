#define GTX_SHIM_DLL "xgameruntime.dll"
#include <windows.h>
#include <dxgi1_6.h>

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "gtx/log.h"
#include "xgr_internal.h"
#include "xtask.h"
#include "handles.h"
#include "ui.h"

namespace gtx::xgr::system {
namespace {

constexpr size_t kAppSpecificDeviceIdBytes = 45;

HRESULT XSystemGetAppSpecificDeviceId_impl(void*, size_t size, char* id, size_t* used) {
  if (used) *used = kAppSpecificDeviceIdBytes;
  if (!id || size < kAppSpecificDeviceIdBytes) return HRESULT_FROM_WIN32(ERROR_INSUFFICIENT_BUFFER);
  std::memset(id, '0', kAppSpecificDeviceIdBytes - 1);
  id[kAppSpecificDeviceIdBytes - 1] = '\0';
  return S_OK;
}

void XSystemAllowFullDownloadBandwidth_impl(void*, uint8_t) {}

struct DisplayTimeoutDeferral {
  HANDLE powerRequest;
};

HRESULT XDisplayAcquireTimeoutDeferral_impl(void*, DisplayTimeoutDeferral** out) {
  if (!out) return E_INVALIDARG;
  *out = nullptr;
  REASON_CONTEXT reason{};
  reason.Version = POWER_REQUEST_CONTEXT_VERSION;
  reason.Flags = POWER_REQUEST_CONTEXT_SIMPLE_STRING;
  reason.Reason.SimpleReasonString = const_cast<wchar_t*>(L"XDisplayAcquireTimeoutDeferral");
  HANDLE request = PowerCreateRequest(&reason);
  if (request == INVALID_HANDLE_VALUE) return HRESULT_FROM_WIN32(GetLastError());
  if (!PowerSetRequest(request, PowerRequestDisplayRequired)) {
    HRESULT hr = HRESULT_FROM_WIN32(GetLastError());
    CloseHandle(request);
    return hr;
  }
  *out = new DisplayTimeoutDeferral{request};
  handles::opened(*out, handles::Type::DisplayTimeoutDeferral);
  return S_OK;
}

void XDisplayCloseTimeoutDeferralHandle_impl(void*, DisplayTimeoutDeferral* deferral) {
  if (!deferral) return;
  handles::closed(deferral);
  PowerClearRequest(deferral->powerRequest, PowerRequestDisplayRequired);
  CloseHandle(deferral->powerRequest);
  delete deferral;
}

enum class HdrModeResult : uint32_t { Unknown, Enabled, Disabled };
constexpr uint32_t kHdrModePreferenceCount = 2;

struct HdrModeInfo {
  float minToneMapLuminance;
  float maxToneMapLuminance;
  float maxFullFrameToneMapLuminance;
};

const IID kIidDxgiFactory1 = {0x770aae78, 0xf26f, 0x4dba, {0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87}};
const IID kIidDxgiOutput6 = {0x068346e8, 0xaaec, 0x4b84, {0xad, 0xd7, 0x13, 0x7f, 0x51, 0x3f, 0x77, 0xa1}};

template <typename T> void release_com(T* object) {
  if (object) object->Release();
}

HdrModeResult query_primary_output_hdr(HdrModeInfo* info) {
  HMODULE dxgi = LoadLibraryW(L"dxgi.dll");
  if (!dxgi) return HdrModeResult::Unknown;
  using CreateFactoryFn = HRESULT(WINAPI*)(REFIID, void**);
  auto create = reinterpret_cast<CreateFactoryFn>(reinterpret_cast<void*>(GetProcAddress(dxgi, "CreateDXGIFactory1")));
  IDXGIFactory1* factory = nullptr;
  HdrModeResult result = HdrModeResult::Unknown;
  if (create && SUCCEEDED(create(kIidDxgiFactory1, reinterpret_cast<void**>(&factory)))) {
    bool found = false;
    IDXGIAdapter1* adapter = nullptr;
    for (UINT a = 0; !found && factory->EnumAdapters1(a, &adapter) != DXGI_ERROR_NOT_FOUND; a++) {
      IDXGIOutput* output = nullptr;
      for (UINT o = 0; !found && adapter->EnumOutputs(o, &output) != DXGI_ERROR_NOT_FOUND; o++) {
        IDXGIOutput6* output6 = nullptr;
        DXGI_OUTPUT_DESC1 desc{};
        if (SUCCEEDED(output->QueryInterface(kIidDxgiOutput6, reinterpret_cast<void**>(&output6))) && SUCCEEDED(output6->GetDesc1(&desc)) &&
            desc.AttachedToDesktop && desc.DesktopCoordinates.left == 0 && desc.DesktopCoordinates.top == 0) {
          found = true;
          if (desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020) {
            result = HdrModeResult::Enabled;
            if (info) *info = {desc.MinLuminance, desc.MaxLuminance, desc.MaxFullFrameLuminance};
          } else {
            result = HdrModeResult::Disabled;
          }
        }
        release_com(output6);
        release_com(output);
      }
      release_com(adapter);
    }
    release_com(factory);
  }
  FreeLibrary(dxgi);
  return result;
}

HdrModeResult XDisplayTryEnableHdrMode_impl(void*, uint32_t preference, HdrModeInfo* info) {
  if (preference >= kHdrModePreferenceCount) return HdrModeResult::Unknown;
  return query_primary_output_hdr(info);
}

using ErrorCallback = bool(CALLBACK*)(HRESULT hr, const char* message, void* context);

constexpr uint32_t kErrorOutputDebugString = 1;
constexpr uint32_t kErrorDebugBreak = 2;

struct ErrorSettings {
  ErrorCallback callback = nullptr;
  void* context = nullptr;
  uint32_t optionsDebuggerPresent = 1;
  uint32_t optionsDebuggerNotPresent = 0;
};

std::mutex g_error_lock;
ErrorSettings g_error_settings;

void XErrorSetCallback_impl(void*, ErrorCallback callback, void* context) {
  std::lock_guard<std::mutex> lock(g_error_lock);
  g_error_settings.callback = callback;
  g_error_settings.context = callback ? context : nullptr;
}

void XErrorSetOptions_impl(void*, uint32_t debuggerPresent, uint32_t debuggerNotPresent) {
  std::lock_guard<std::mutex> lock(g_error_lock);
  g_error_settings.optionsDebuggerPresent = debuggerPresent;
  g_error_settings.optionsDebuggerNotPresent = debuggerNotPresent;
}

std::atomic<uint64_t> g_next_token{1};

HRESULT register_silent_event(const void* callback, task::RegistrationToken* token) {
  if (!callback || !token) return E_INVALIDARG;
  token->token = g_next_token++;
  return S_OK;
}

uint8_t unregister_silent_event(task::RegistrationToken, uint8_t) { return 1; }

HRESULT XGameProtocolRegisterForActivation_impl(void*, task::Queue*, void*, const void* callback, task::RegistrationToken* token) {
  return register_silent_event(callback, token);
}
uint8_t XGameProtocolUnregisterForActivation_impl(void*, task::RegistrationToken token, uint8_t wait) {
  return unregister_silent_event(token, wait);
}
HRESULT XGameActivationRegisterForEvent_impl(void*, task::Queue*, void*, const void* callback, task::RegistrationToken* token) {
  return register_silent_event(callback, token);
}
uint8_t XGameActivationUnregisterForEvent_impl(void*, task::RegistrationToken token, uint8_t wait) {
  return unregister_silent_event(token, wait);
}
HRESULT XGameInviteRegisterForEvent_impl(void*, task::Queue*, void*, const void* callback, task::RegistrationToken* token) {
  return register_silent_event(callback, token);
}
uint8_t XGameInviteUnregisterForEvent_impl(void*, task::RegistrationToken token, uint8_t wait) {
  return unregister_silent_event(token, wait);
}
HRESULT XGameInviteRegisterForPendingEvent_impl(void*, task::Queue*, void*, const void* callback, task::RegistrationToken* token) {
  return register_silent_event(callback, token);
}
uint8_t XGameInviteUnregisterForPendingEvent_impl(void*, task::RegistrationToken token, uint8_t wait) {
  return unregister_silent_event(token, wait);
}

struct ConnectivityHint {
  uint32_t connectivityLevel;
  uint32_t connectivityCost;
  uint32_t ianaInterfaceType;
  uint8_t networkInitialized;
  uint8_t approachingDataLimit;
  uint8_t overDataLimit;
  uint8_t roaming;
};
static_assert(sizeof(ConnectivityHint) == 16, "XNetworkingConnectivityHint layout");

constexpr uint32_t kConnectivityLevelNone = 1;
constexpr uint32_t kConnectivityCostUnknown = 0;
const ConnectivityHint kOfflineHint{kConnectivityLevelNone, kConnectivityCostUnknown, 0, 1, 0, 0, 0};

HRESULT XNetworkingGetConnectivityHint_impl(void*, ConnectivityHint* hint) {
  if (!hint) return E_INVALIDARG;
  *hint = kOfflineHint;
  return S_OK;
}

using ConnectivityHintChangedCallback = void(CALLBACK*)(void* context, const ConnectivityHint* hint);

struct ConnectivityRegistration {
  void* context;
  ConnectivityHintChangedCallback callback;
  task::Queue* queue;
  bool active;
  bool running;
  uint64_t token;
};

std::mutex g_connectivity_lock;
std::condition_variable g_connectivity_idle;
std::unordered_map<uint64_t, std::shared_ptr<ConnectivityRegistration>> g_connectivity;

void CALLBACK deliver_initial_connectivity_hint(void* holder, bool canceled) {
  auto* registration = static_cast<std::shared_ptr<ConnectivityRegistration>*>(holder);
  ConnectivityRegistration& r = **registration;
  bool deliver;
  {
    std::lock_guard<std::mutex> lock(g_connectivity_lock);
    deliver = r.active && !canceled;
    r.running = deliver;
  }
  if (deliver) {
    r.callback(r.context, &kOfflineHint);
    std::lock_guard<std::mutex> lock(g_connectivity_lock);
    r.running = false;
    if (!r.active) g_connectivity.erase(r.token);
    g_connectivity_idle.notify_all();
  }
  task::Close(r.queue);
  delete registration;
}

HRESULT XNetworkingRegisterConnectivityHintChanged_impl(void*, task::Queue* queue, void* context, ConnectivityHintChangedCallback callback,
                                                        task::RegistrationToken* token) {
  if (!callback || !token) return E_INVALIDARG;
  task::Queue* target = nullptr;
  if (queue) {
    HRESULT hr = task::Duplicate(queue, &target);
    if (FAILED(hr)) return hr;
  } else if (!task::GetProcessQueue(&target)) {
    return E_INVALIDARG;
  }
  uint64_t value = g_next_token++;
  auto registration = std::make_shared<ConnectivityRegistration>(ConnectivityRegistration{context, callback, target, true, false, value});
  {
    std::lock_guard<std::mutex> lock(g_connectivity_lock);
    g_connectivity.emplace(value, registration);
  }
  auto* holder = new std::shared_ptr<ConnectivityRegistration>(registration);
  HRESULT hr = task::Submit(target, task::PortId::Completion, holder, deliver_initial_connectivity_hint);
  if (FAILED(hr)) {
    delete holder;
    task::Close(target);
    std::lock_guard<std::mutex> lock(g_connectivity_lock);
    g_connectivity.erase(value);
    return hr;
  }
  token->token = value;
  return S_OK;
}

uint8_t XNetworkingUnregisterConnectivityHintChanged_impl(void*, task::RegistrationToken token, uint8_t wait) {
  std::unique_lock<std::mutex> lock(g_connectivity_lock);
  auto found = g_connectivity.find(token.token);
  if (found == g_connectivity.end()) return 1;
  std::shared_ptr<ConnectivityRegistration> registration = found->second;
  registration->active = false;
  if (!registration->running) {
    g_connectivity.erase(found);
    return 1;
  }
  if (!wait) return 0;
  g_connectivity_idle.wait(lock, [&] { return !registration->running; });
  return 1;
}

constexpr uint32_t kNetworkingConfigurationSettingCount = 3;
constexpr uint32_t kNetworkingStatisticsTypeCount = 3;

struct TcpQueuedReceivedBufferUsageStatistics {
  uint64_t numBytesCurrentlyQueued;
  uint64_t peakNumBytesEverQueued;
  uint64_t totalNumBytesQueued;
  uint64_t numBytesDroppedForExceedingConfiguredMax;
  uint64_t numBytesDroppedDueToAnyFailure;
};

HRESULT XNetworkingQueryConfigurationSetting_impl(void*, uint32_t setting, uint64_t* value) {
  if (setting >= kNetworkingConfigurationSettingCount || !value) return E_INVALIDARG;
  *value = 0;
  return S_OK;
}

HRESULT XNetworkingSetConfigurationSetting_impl(void*, uint32_t setting, uint64_t) {
  if (setting >= kNetworkingConfigurationSettingCount) return E_INVALIDARG;
  return E_NOTIMPL;
}

HRESULT XNetworkingQueryStatistics_impl(void*, uint32_t type, TcpQueuedReceivedBufferUsageStatistics* statistics) {
  if (type >= kNetworkingStatisticsTypeCount || !statistics) return E_INVALIDARG;
  *statistics = {};
  return S_OK;
}

constexpr uint32_t kHighContrastOff = 0;
constexpr uint32_t kHighContrastDark = 1;
constexpr uint32_t kHighContrastLight = 2;

HRESULT XHighContrastGetMode_impl(void*, uint32_t* mode) {
  if (!mode) return E_INVALIDARG;
  HIGHCONTRASTW contrast{sizeof(contrast)};
  if (!SystemParametersInfoW(SPI_GETHIGHCONTRAST, sizeof(contrast), &contrast, 0)) return HRESULT_FROM_WIN32(GetLastError());
  if (!(contrast.dwFlags & HCF_HIGHCONTRASTON)) {
    *mode = kHighContrastOff;
    return S_OK;
  }
  COLORREF window = GetSysColor(COLOR_WINDOW);
  unsigned luminance = (299u * GetRValue(window) + 587u * GetGValue(window) + 114u * GetBValue(window)) / 1000u;
  *mode = luminance < 128 ? kHighContrastDark : kHighContrastLight;
  return S_OK;
}

struct FeatureRequirement {
  uint32_t feature;
  const char* apis[8];
};

const FeatureRequirement kFeatureRequirements[] = {
    {0, {"XClosedCaptionGetProperties", "XHighContrastGetMode"}},
    {2, {"XAsyncGetStatus", "XAsyncGetResultSize", "XAsyncCancel", "XAsyncRun", "XAsyncGetResult"}},
    {3, {"XAsyncBegin", "XAsyncSchedule", "XAsyncComplete", "XAsyncGetResult"}},
    {4, {"XDisplayAcquireTimeoutDeferral", "XDisplayCloseTimeoutDeferralHandle", "XDisplayTryEnableHdrMode"}},
    {5, {"XGameGetXboxTitleId", "XLaunchNewGame"}},
    {6, {"XGameInviteRegisterForEvent", "XGameInviteUnregisterForEvent"}},
    {7, {"XGameSaveInitializeProvider", "XGameSaveCreateContainer", "XGameSaveCreateUpdate", "XGameSaveSubmitUpdate",
         "XGameSaveReadBlobData", "XGameSaveCloseContainer", "XGameSaveCloseProvider"}},
    {8, {"XGameUiShowMessageDialogAsync", "XGameUiShowMessageDialogResult"}},
    {9, {"XLaunchUri"}},
    {10, {"XNetworkingGetConnectivityHint", "XNetworkingRegisterConnectivityHintChanged", "XNetworkingUnregisterConnectivityHintChanged"}},
    {11, {"XPackageGetCurrentProcessPackageIdentifier", "XPackageIsPackagedProcess"}},
    {12, {"XPersistentLocalStorageGetPathSize", "XPersistentLocalStorageGetPath"}},
    {14, {"XStoreCreateContext", "XStoreQueryGameLicenseAsync", "XStoreQueryGameLicenseResult"}},
    {15, {"XSystemGetAnalyticsInfo", "XSystemGetConsoleId"}},
    {16, {"XTaskQueueCreate", "XTaskQueueDispatch", "XTaskQueueCloseHandle", "XTaskQueueSubmitCallback",
          "XTaskQueueGetCurrentProcessTaskQueue"}},
    {17, {"XThreadIsTimeSensitive", "XThreadSetTimeSensitive", "XThreadAssertNotTimeSensitive"}},
    {18, {"XUserAddAsync", "XUserAddResult", "XUserGetId", "XUserGetLocalId", "XUserGetState", "XUserCloseHandle"}},
    {19, {"XErrorSetCallback", "XErrorSetOptions"}},
    {20, {"XGameEventWrite"}},
    {21, {"XGameStreamingInitialize", "XGameStreamingIsStreaming"}},
};

bool is_implemented(const char* api) {
  size_t count = 0;
  const Override* all = gtx::xgr::overrides(&count);
  for (size_t i = 0; i < count; i++)
    if (!std::strcmp(all[i].api, api)) return true;
  return false;
}

uint8_t XGameRuntimeIsFeatureAvailable_impl(void*, uint32_t feature) {
  for (const FeatureRequirement& requirement : kFeatureRequirements) {
    if (requirement.feature != feature) continue;
    for (const char* api : requirement.apis)
      if (api && !is_implemented(api)) return 0;
    return 1;
  }
  return 0;
}

HRESULT CALLBACK no_ui_provider(task::AsyncOp op, const task::AsyncProviderData*) {
  return op == task::AsyncOp::DoWork ? E_ABORT : S_OK;
}

HRESULT begin_without_ui(task::AsyncBlock* async, const void* identity, const char* name) {
  HRESULT hr = task::AsyncBegin(async, nullptr, identity, name, no_ui_provider);
  if (FAILED(hr)) return hr;
  return task::AsyncSchedule(async, 0);
}

HRESULT result_without_payload(task::AsyncBlock* async, const void* identity) {
  return task::AsyncGetResult(async, identity, 0, nullptr, nullptr);
}

std::optional<std::string> keep(const char* text) {
  if (!text) return std::nullopt;
  return std::string(text);
}

const char* text_of(const std::optional<std::string>& text) { return text ? text->c_str() : nullptr; }

HRESULT XGameUiShowMessageDialogAsync_impl(void*, task::AsyncBlock* async, const char* title, const char* content, const char* first,
                                           const char* second, const char* third, uint32_t defaultButton, uint32_t cancelButton) {
  if (!async || !title || !content || defaultButton > 2 || cancelButton > 2) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowMessageDialogAsync_impl);
  ui::Callbacks callbacks = ui::title_callbacks();
  if (!callbacks.showMessageDialog) return begin_without_ui(async, identity, "XGameUiShowMessageDialogAsync");
  auto show = [callbacks, title = std::string(title), content = std::string(content), first = keep(first), second = keep(second),
               third = keep(third), defaultButton, cancelButton](void* handle, task::Queue* queue) {
    callbacks.showMessageDialog(handle, queue, title.c_str(), content.c_str(), text_of(first), text_of(second), text_of(third), defaultButton,
                                cancelButton, callbacks.context);
  };
  return ui::begin_title_ui(async, identity, "XGameUiShowMessageDialogAsync", ui::Request::MessageDialog, show);
}
HRESULT XGameUiShowMessageDialogResult_impl(void*, task::AsyncBlock* async, uint32_t* button) {
  if (!button) return E_INVALIDARG;
  return task::AsyncGetResult(async, reinterpret_cast<const void*>(&XGameUiShowMessageDialogAsync_impl), sizeof(*button), button, nullptr);
}

HRESULT XGameUiShowSendGameInviteAsync_impl(void*, task::AsyncBlock* async, void* user, const char* scid, const char* templateName,
                                            const char* sessionId, const char* invitationText, const char* customActivationContext) {
  if (!async || !user || !scid || !templateName || !sessionId) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowSendGameInviteAsync_impl);
  ui::Callbacks callbacks = ui::title_callbacks();
  if (!callbacks.showSendGameInvite) return begin_without_ui(async, identity, "XGameUiShowSendGameInviteAsync");
  auto show = [callbacks, user, scid = std::string(scid), templateName = std::string(templateName), sessionId = std::string(sessionId),
               invitationText = keep(invitationText), customActivationContext = keep(customActivationContext)](void* handle, task::Queue* queue) {
    callbacks.showSendGameInvite(handle, queue, user, scid.c_str(), templateName.c_str(), sessionId.c_str(), text_of(invitationText),
                                 text_of(customActivationContext), callbacks.context);
  };
  return ui::begin_title_ui(async, identity, "XGameUiShowSendGameInviteAsync", ui::Request::SendGameInvite, show);
}
HRESULT XGameUiShowSendGameInviteResult_impl(void*, task::AsyncBlock* async) {
  return result_without_payload(async, reinterpret_cast<const void*>(&XGameUiShowSendGameInviteAsync_impl));
}

HRESULT XGameUiShowPlayerProfileCardAsync_impl(void*, task::AsyncBlock* async, void* user, uint64_t targetPlayer) {
  if (!async || !user) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowPlayerProfileCardAsync_impl);
  ui::Callbacks callbacks = ui::title_callbacks();
  if (!callbacks.showPlayerProfileCard) return begin_without_ui(async, identity, "XGameUiShowPlayerProfileCardAsync");
  auto show = [callbacks, user, targetPlayer](void* handle, task::Queue* queue) {
    callbacks.showPlayerProfileCard(handle, queue, user, targetPlayer, callbacks.context);
  };
  return ui::begin_title_ui(async, identity, "XGameUiShowPlayerProfileCardAsync", ui::Request::PlayerProfileCard, show);
}
HRESULT XGameUiShowPlayerProfileCardResult_impl(void*, task::AsyncBlock* async) {
  return result_without_payload(async, reinterpret_cast<const void*>(&XGameUiShowPlayerProfileCardAsync_impl));
}

HRESULT XGameUiShowAchievementsAsync_impl(void*, task::AsyncBlock* async, void* user, uint32_t titleId) {
  if (!async || !user) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowAchievementsAsync_impl);
  ui::Callbacks callbacks = ui::title_callbacks();
  if (!callbacks.showAchievements) return begin_without_ui(async, identity, "XGameUiShowAchievementsAsync");
  auto show = [callbacks, user, titleId](void* handle, task::Queue* queue) {
    callbacks.showAchievements(handle, queue, user, titleId, callbacks.context);
  };
  return ui::begin_title_ui(async, identity, "XGameUiShowAchievementsAsync", ui::Request::Achievements, show);
}
HRESULT XGameUiShowAchievementsResult_impl(void*, task::AsyncBlock* async) {
  return result_without_payload(async, reinterpret_cast<const void*>(&XGameUiShowAchievementsAsync_impl));
}

HRESULT XGameUiShowPlayerPickerAsync_impl(void*, task::AsyncBlock* async, void* user, const char* prompt, uint32_t selectFromCount,
                                          const uint64_t* selectFrom, uint32_t preSelectedCount, const uint64_t* preSelected, uint32_t minSelection,
                                          uint32_t maxSelection) {
  if (!async || !user || !prompt || (selectFromCount && !selectFrom) || (preSelectedCount && !preSelected)) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowPlayerPickerAsync_impl);
  ui::Callbacks callbacks = ui::title_callbacks();
  if (!callbacks.showPlayerPicker) return begin_without_ui(async, identity, "XGameUiShowPlayerPickerAsync");
  auto show = [callbacks, user, prompt = std::string(prompt), selectFrom = std::vector<uint64_t>(selectFrom, selectFrom + selectFromCount),
               preSelected = std::vector<uint64_t>(preSelected, preSelected + preSelectedCount), minSelection,
               maxSelection](void* handle, task::Queue* queue) {
    ui::PlayerPickerInfo info{user,
                              prompt.c_str(),
                              static_cast<uint32_t>(selectFrom.size()),
                              selectFrom.data(),
                              static_cast<uint32_t>(preSelected.size()),
                              preSelected.data(),
                              minSelection,
                              maxSelection};
    callbacks.showPlayerPicker(handle, queue, &info, callbacks.context);
  };
  return ui::begin_title_ui(async, identity, "XGameUiShowPlayerPickerAsync", ui::Request::PlayerPicker, show);
}
HRESULT XGameUiShowPlayerPickerResultCount_impl(void*, task::AsyncBlock* async, uint32_t* count) {
  if (!count) return E_INVALIDARG;
  size_t bytes = 0;
  HRESULT hr = task::AsyncGetResultSize(async, &bytes);
  if (SUCCEEDED(hr)) *count = static_cast<uint32_t>(bytes / sizeof(uint64_t));
  return hr;
}
HRESULT XGameUiShowPlayerPickerResult_impl(void*, task::AsyncBlock* async, uint32_t count, uint64_t* players, uint32_t* used) {
  size_t bytes = 0;
  HRESULT hr = task::AsyncGetResult(async, reinterpret_cast<const void*>(&XGameUiShowPlayerPickerAsync_impl), sizeof(uint64_t) * count, players,
                                    &bytes);
  if (SUCCEEDED(hr) && used) *used = static_cast<uint32_t>(bytes / sizeof(uint64_t));
  return hr;
}

HRESULT XGameUiShowErrorDialogAsync_impl(void*, task::AsyncBlock* async, HRESULT errorCode, const char* serviceContext) {
  if (!async) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowErrorDialogAsync_impl);
  ui::Callbacks callbacks = ui::title_callbacks();
  if (!callbacks.showErrorDialog) return begin_without_ui(async, identity, "XGameUiShowErrorDialogAsync");
  auto show = [callbacks, errorCode, serviceContext = keep(serviceContext)](void* handle, task::Queue* queue) {
    callbacks.showErrorDialog(handle, queue, errorCode, text_of(serviceContext), callbacks.context);
  };
  return ui::begin_title_ui(async, identity, "XGameUiShowErrorDialogAsync", ui::Request::ErrorDialog, show);
}
HRESULT XGameUiShowErrorDialogResult_impl(void*, task::AsyncBlock* async) {
  return result_without_payload(async, reinterpret_cast<const void*>(&XGameUiShowErrorDialogAsync_impl));
}

HRESULT XGameUiSetNotificationPositionHint_impl(void*, uint32_t position) {
  return position <= 5 ? S_OK : E_INVALIDARG;
}

HRESULT XGameUiShowTextEntryAsync_impl(void*, task::AsyncBlock* async, const char* title, const char* description, const char* defaultText,
                                       uint32_t inputScope, uint32_t maxTextLength) {
  if (!async) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowTextEntryAsync_impl);
  ui::Callbacks callbacks = ui::title_callbacks();
  if (!callbacks.showTextEntry) return begin_without_ui(async, identity, "XGameUiShowTextEntryAsync");
  auto show = [callbacks, title = keep(title), description = keep(description), defaultText = keep(defaultText), inputScope,
               maxTextLength](void* handle, task::Queue* queue) {
    callbacks.showTextEntry(handle, queue, text_of(title), text_of(description), text_of(defaultText), inputScope, maxTextLength,
                            callbacks.context);
  };
  return ui::begin_title_ui(async, identity, "XGameUiShowTextEntryAsync", ui::Request::TextEntry, show);
}
HRESULT XGameUiShowTextEntryResultSize_impl(void*, task::AsyncBlock* async, uint32_t* size) {
  if (!size) return E_INVALIDARG;
  size_t bytes = 0;
  HRESULT hr = task::AsyncGetResultSize(async, &bytes);
  if (SUCCEEDED(hr)) *size = static_cast<uint32_t>(bytes);
  return hr;
}
HRESULT XGameUiShowTextEntryResult_impl(void*, task::AsyncBlock* async, uint32_t size, char* text, uint32_t* used) {
  size_t bytes = 0;
  HRESULT hr = task::AsyncGetResult(async, reinterpret_cast<const void*>(&XGameUiShowTextEntryAsync_impl), size, text, &bytes);
  if (SUCCEEDED(hr) && used) *used = static_cast<uint32_t>(bytes);
  return hr;
}

HRESULT XGameUiShowMultiplayerActivityGameInviteAsync_impl(void*, task::AsyncBlock* async, void* user) {
  if (!async || !user) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowMultiplayerActivityGameInviteAsync_impl);
  ui::Callbacks callbacks = ui::title_callbacks();
  if (!callbacks.showMultiplayerActivityGameInvite) return begin_without_ui(async, identity, "XGameUiShowMultiplayerActivityGameInviteAsync");
  auto show = [callbacks, user](void* handle, task::Queue* queue) {
    callbacks.showMultiplayerActivityGameInvite(handle, queue, user, callbacks.context);
  };
  return ui::begin_title_ui(async, identity, "XGameUiShowMultiplayerActivityGameInviteAsync", ui::Request::MultiplayerActivityGameInvite, show);
}
HRESULT XGameUiShowMultiplayerActivityGameInviteResult_impl(void*, task::AsyncBlock* async) {
  return result_without_payload(async, reinterpret_cast<const void*>(&XGameUiShowMultiplayerActivityGameInviteAsync_impl));
}

HRESULT XGameUiShowStateShareAsync_impl(void*, task::AsyncBlock* async, void* user, const char* linkToken) {
  if (!async || !user || !linkToken) return E_INVALIDARG;
  const void* identity = reinterpret_cast<const void*>(&XGameUiShowStateShareAsync_impl);
  return begin_without_ui(async, identity, "XGameUiShowStateShareAsync");
}
HRESULT XGameUiShowStateShareResult_impl(void*, task::AsyncBlock* async) {
  return result_without_payload(async, reinterpret_cast<const void*>(&XGameUiShowStateShareAsync_impl));
}

struct GameUiTextEntryOptions {
  uint32_t inputScope;
  uint32_t positionHint;
  uint32_t visibilityFlags;
};

struct GameUiTextEntryExtents {
  float left;
  float top;
  float right;
  float bottom;
};

struct GameUiTextEntry {
  uint32_t inputScope = 0;
  uint32_t positionHint = 0;
  uint32_t visibilityFlags = 0;
  uint32_t maxLength = 0;
  uint32_t cursorIndex = 0;
  std::string text;
};

HRESULT XGameUiTextEntryOpen_impl(void*, const GameUiTextEntryOptions* options, const char* initialText, uint32_t initialCursorIndex,
                                  uint32_t maxLength, GameUiTextEntry** handle) {
  if (!handle) return E_INVALIDARG;
  *handle = nullptr;
  auto entry = new GameUiTextEntry();
  if (options) {
    entry->inputScope = options->inputScope;
    entry->positionHint = options->positionHint;
    entry->visibilityFlags = options->visibilityFlags;
  }
  if (initialText) entry->text = initialText;
  entry->cursorIndex = initialCursorIndex;
  entry->maxLength = maxLength;
  *handle = entry;
  handles::opened(*handle, handles::Type::GameUiTextEntry);
  return S_OK;
}

HRESULT XGameUiTextEntryClose_impl(void*, GameUiTextEntry* handle) {
  if (!handle) return E_INVALIDARG;
  handles::closed(handle);
  delete handle;
  return S_OK;
}

HRESULT XGameUiTextEntryGetState_impl(void*, GameUiTextEntry* handle, uint32_t* changeType, uint32_t* cursorIndex,
                                      uint32_t* imeClauseStartIndex, uint32_t* imeClauseEndIndex, uint32_t bufferSize, char* buffer) {
  if (!handle || !changeType || !cursorIndex || !imeClauseStartIndex || !imeClauseEndIndex) return E_INVALIDARG;
  if (bufferSize && !buffer) return E_INVALIDARG;
  *changeType = 0;
  *cursorIndex = handle->cursorIndex;
  *imeClauseStartIndex = 0;
  *imeClauseEndIndex = 0;
  if (buffer && bufferSize) {
    size_t copyLen = (std::min)(static_cast<size_t>(bufferSize - 1), handle->text.size());
    std::memcpy(buffer, handle->text.c_str(), copyLen);
    buffer[copyLen] = '\0';
  }
  return S_OK;
}

HRESULT XGameUiTextEntryGetExtents_impl(void*, GameUiTextEntry* handle, GameUiTextEntryExtents* extents) {
  if (!handle || !extents) return E_INVALIDARG;
  extents->left = 0.0f;
  extents->top = 0.0f;
  extents->right = 0.0f;
  extents->bottom = 0.0f;
  return S_OK;
}

HRESULT XGameUiTextEntryUpdatePositionHint_impl(void*, GameUiTextEntry* handle, uint32_t positionHint) {
  if (!handle) return E_INVALIDARG;
  handle->positionHint = positionHint;
  return S_OK;
}

HRESULT XGameUiTextEntryUpdateVisibility_impl(void*, GameUiTextEntry* handle, uint32_t visibilityFlags) {
  if (!handle) return E_INVALIDARG;
  handle->visibilityFlags = visibilityFlags;
  return S_OK;
}

const Override kOverrides[] = {
    {"XSystemGetAppSpecificDeviceId", reinterpret_cast<void*>(&XSystemGetAppSpecificDeviceId_impl)},
    {"XSystemAllowFullDownloadBandwidth", reinterpret_cast<void*>(&XSystemAllowFullDownloadBandwidth_impl)},
    {"XDisplayTryEnableHdrMode", reinterpret_cast<void*>(&XDisplayTryEnableHdrMode_impl)},
    {"XErrorSetCallback", reinterpret_cast<void*>(&XErrorSetCallback_impl)},
    {"XErrorSetOptions", reinterpret_cast<void*>(&XErrorSetOptions_impl)},
    {"XDisplayAcquireTimeoutDeferral", reinterpret_cast<void*>(&XDisplayAcquireTimeoutDeferral_impl)},
    {"XDisplayCloseTimeoutDeferralHandle", reinterpret_cast<void*>(&XDisplayCloseTimeoutDeferralHandle_impl)},
    {"XGameRuntimeIsFeatureAvailable", reinterpret_cast<void*>(&XGameRuntimeIsFeatureAvailable_impl)},
    {"XGameProtocolRegisterForActivation", reinterpret_cast<void*>(&XGameProtocolRegisterForActivation_impl)},
    {"XGameProtocolUnregisterForActivation", reinterpret_cast<void*>(&XGameProtocolUnregisterForActivation_impl)},
    {"XGameActivationRegisterForEvent", reinterpret_cast<void*>(&XGameActivationRegisterForEvent_impl)},
    {"XGameActivationUnregisterForEvent", reinterpret_cast<void*>(&XGameActivationUnregisterForEvent_impl)},
    {"XGameInviteRegisterForEvent", reinterpret_cast<void*>(&XGameInviteRegisterForEvent_impl)},
    {"XGameInviteUnregisterForEvent", reinterpret_cast<void*>(&XGameInviteUnregisterForEvent_impl)},
    {"XGameInviteRegisterForPendingEvent", reinterpret_cast<void*>(&XGameInviteRegisterForPendingEvent_impl)},
    {"XGameInviteUnregisterForPendingEvent", reinterpret_cast<void*>(&XGameInviteUnregisterForPendingEvent_impl)},
    {"XNetworkingGetConnectivityHint", reinterpret_cast<void*>(&XNetworkingGetConnectivityHint_impl)},
    {"XNetworkingRegisterConnectivityHintChanged", reinterpret_cast<void*>(&XNetworkingRegisterConnectivityHintChanged_impl)},
    {"XNetworkingUnregisterConnectivityHintChanged", reinterpret_cast<void*>(&XNetworkingUnregisterConnectivityHintChanged_impl)},
    {"XNetworkingQueryConfigurationSetting", reinterpret_cast<void*>(&XNetworkingQueryConfigurationSetting_impl)},
    {"XNetworkingSetConfigurationSetting", reinterpret_cast<void*>(&XNetworkingSetConfigurationSetting_impl)},
    {"XNetworkingQueryStatistics", reinterpret_cast<void*>(&XNetworkingQueryStatistics_impl)},
    {"XHighContrastGetMode", reinterpret_cast<void*>(&XHighContrastGetMode_impl)},
    {"XGameUiShowMessageDialogAsync", reinterpret_cast<void*>(&XGameUiShowMessageDialogAsync_impl)},
    {"XGameUiShowMessageDialogResult", reinterpret_cast<void*>(&XGameUiShowMessageDialogResult_impl)},
    {"XGameUiShowSendGameInviteAsync", reinterpret_cast<void*>(&XGameUiShowSendGameInviteAsync_impl)},
    {"XGameUiShowSendGameInviteResult", reinterpret_cast<void*>(&XGameUiShowSendGameInviteResult_impl)},
    {"XGameUiShowPlayerProfileCardAsync", reinterpret_cast<void*>(&XGameUiShowPlayerProfileCardAsync_impl)},
    {"XGameUiShowPlayerProfileCardResult", reinterpret_cast<void*>(&XGameUiShowPlayerProfileCardResult_impl)},
    {"XGameUiShowAchievementsAsync", reinterpret_cast<void*>(&XGameUiShowAchievementsAsync_impl)},
    {"XGameUiShowAchievementsResult", reinterpret_cast<void*>(&XGameUiShowAchievementsResult_impl)},
    {"XGameUiShowPlayerPickerAsync", reinterpret_cast<void*>(&XGameUiShowPlayerPickerAsync_impl)},
    {"XGameUiShowPlayerPickerResultCount", reinterpret_cast<void*>(&XGameUiShowPlayerPickerResultCount_impl)},
    {"XGameUiShowPlayerPickerResult", reinterpret_cast<void*>(&XGameUiShowPlayerPickerResult_impl)},
    {"XGameUiShowErrorDialogAsync", reinterpret_cast<void*>(&XGameUiShowErrorDialogAsync_impl)},
    {"XGameUiShowErrorDialogResult", reinterpret_cast<void*>(&XGameUiShowErrorDialogResult_impl)},
    {"XGameUiSetNotificationPositionHint", reinterpret_cast<void*>(&XGameUiSetNotificationPositionHint_impl)},
    {"XGameUiShowTextEntryAsync", reinterpret_cast<void*>(&XGameUiShowTextEntryAsync_impl)},
    {"XGameUiShowTextEntryResultSize", reinterpret_cast<void*>(&XGameUiShowTextEntryResultSize_impl)},
    {"XGameUiShowTextEntryResult", reinterpret_cast<void*>(&XGameUiShowTextEntryResult_impl)},
    {"XGameUiShowMultiplayerActivityGameInviteAsync", reinterpret_cast<void*>(&XGameUiShowMultiplayerActivityGameInviteAsync_impl)},
    {"XGameUiShowMultiplayerActivityGameInviteResult", reinterpret_cast<void*>(&XGameUiShowMultiplayerActivityGameInviteResult_impl)},
    {"XGameUiTextEntryOpen", reinterpret_cast<void*>(&XGameUiTextEntryOpen_impl)},
    {"XGameUiTextEntryClose", reinterpret_cast<void*>(&XGameUiTextEntryClose_impl)},
    {"XGameUiTextEntryGetState", reinterpret_cast<void*>(&XGameUiTextEntryGetState_impl)},
    {"XGameUiTextEntryGetExtents", reinterpret_cast<void*>(&XGameUiTextEntryGetExtents_impl)},
    {"XGameUiTextEntryUpdatePositionHint", reinterpret_cast<void*>(&XGameUiTextEntryUpdatePositionHint_impl)},
    {"XGameUiTextEntryUpdateVisibility", reinterpret_cast<void*>(&XGameUiTextEntryUpdateVisibility_impl)},
    {"XGameUiShowStateShareAsync", reinterpret_cast<void*>(&XGameUiShowStateShareAsync_impl)},
    {"XGameUiShowStateShareResult", reinterpret_cast<void*>(&XGameUiShowStateShareResult_impl)},
};

}  // namespace

void report_error(HRESULT hr, const char* message) {
  ErrorSettings settings;
  {
    std::lock_guard<std::mutex> lock(g_error_lock);
    settings = g_error_settings;
  }
  const char* text = message ? message : "";
  if (settings.callback && !settings.callback(hr, text, settings.context)) return;
  uint32_t options = IsDebuggerPresent() ? settings.optionsDebuggerPresent : settings.optionsDebuggerNotPresent;
  if (options & (kErrorOutputDebugString | kErrorDebugBreak)) {
    char line[512];
    std::snprintf(line, sizeof(line), "XErrorReport %08lx thread %lu: %s\n", static_cast<unsigned long>(hr), GetCurrentThreadId(), text);
    OutputDebugStringA(line);
  }
}

const Override* overrides(size_t* count) {
  *count = sizeof(kOverrides) / sizeof(kOverrides[0]);
  return kOverrides;
}

}  // namespace gtx::xgr::system
