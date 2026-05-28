//+------------------------------------------------------------------+
//| BB_RSI_MeanReversion.mq5                                         |
//| Version: 1.0                                                      |
//| Description: Mean reversion EA using Bollinger Bands + RSI       |
//|              on M1 with optional M15 higher-timeframe context.   |
//|                                                                  |
//| RISK WARNING: This EA is for educational purposes only.          |
//| Live trading requires proper risk assessment, forward testing,   |
//| and understanding of all risks involved in Forex trading.        |
//| Past performance does not guarantee future results.             |
//+------------------------------------------------------------------+
#property copyright "GENESIS Strategy B — Ares"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//+------------------------------------------------------------------+
//| INPUT GROUPS                                                      |
//+------------------------------------------------------------------+

// --- 1. Trade Filters ---
input string   Inp_TradeComment             = "BB_RSI_M1";   // EA comment
input bool     Inp_AllowLong                = true;           // Allow long trades
input bool     Inp_AllowShort               = true;           // Allow short trades
input int      Inp_MagicNumber              = 20250514;       // EA magic number

// --- 2. Bollinger Bands ---
input int              Inp_BB_Period        = 20;             // BB period
input double           Inp_BB_Deviation     = 2.0;            // BB deviation
input int              Inp_BB_Shift         = 0;              // BB shift
input ENUM_MA_METHOD   Inp_BB_MA_Method     = MODE_SMA;       // BB MA method
input ENUM_APPLIED_PRICE Inp_BB_Price       = PRICE_CLOSE;    // BB applied price

// --- 3. RSI ---
input int              Inp_RSI_Period       = 14;             // RSI period
input double           Inp_RSI_Oversold     = 30.0;           // RSI oversold level
input double           Inp_RSI_Overbought   = 70.0;           // RSI overbought level
input ENUM_APPLIED_PRICE Inp_RSI_Price      = PRICE_CLOSE;    // RSI applied price

// --- 4. Higher Timeframe Context (M15) ---
input bool             Inp_UseM15Context    = true;           // Use M15 context
input ENUM_TIMEFRAMES  Inp_ContextTF        = PERIOD_M15;     // Context timeframe
input int              Inp_ContextMAPeriod  = 50;             // Context MA period
input double           Inp_ContextMATol     = 0.0002;         // Distance tolerance from MA

// --- 5. Entry Logic ---
input bool   Inp_RequireOutsideBand         = true;           // Price must close outside BB
input bool   Inp_RequireRSIFilter           = true;           // Require RSI filter
input int    Inp_CandlesSinceSignal         = 1;              // Candle index (1=last closed)

// --- 6. Risk & Money Management ---
input double Inp_RiskPercent                = 1.0;            // % account risked per trade
input bool   Inp_UseFixedLot               = false;           // Use fixed lot
input double Inp_FixedLot                  = 0.01;            // Fixed lot size
input int    Inp_StopLossPips              = 20;              // SL in pips
input int    Inp_TakeProfitPips            = 40;              // TP in pips
input bool   Inp_UseTrailingStop           = false;           // Enable trailing stop
input int    Inp_TrailingStartPips         = 15;              // Profit pips to start trailing
input int    Inp_TrailingStepPips          = 5;               // Trailing step in pips

// --- 7. Time & Session Filters ---
input bool   Inp_UseTimeFilter             = true;            // Restrict trading hours
input int    Inp_StartHour                 = 5;               // Start hour (GMT)
input int    Inp_StartMinute               = 0;               // Start minute
input int    Inp_EndHour                   = 17;              // End hour (GMT)
input int    Inp_EndMinute                 = 0;               // End minute
input bool   Inp_UseNewsFilter             = true;            // Avoid news events
input string Inp_NewsFile                  = "news.txt";      // News timestamps file

// --- 8. Spread & Slippage ---
input double Inp_MaxSpreadPips             = 1.0;             // Max allowed spread (pips)
input int    Inp_Slippage                  = 10;              // Slippage tolerance (points)
input int    Inp_MaxRetries                = 3;               // Max order send retries

// --- 9. Drawdown Protection ---
input bool   Inp_UseDailyLossLimit         = true;            // Stop after daily loss
input double Inp_DailyLossPercent          = 6.0;             // Max daily loss %
input bool   Inp_UseGlobalDDLimit          = true;            // Global drawdown halt
input double Inp_GlobalDDPercent           = 25.0;            // Max total DD %
input bool   Inp_CloseAllOnDD              = true;            // Close all on DD breach

// --- 10. Execution ---
input bool   Inp_UseOnePositionPerDir      = true;            // One position per direction
input int    Inp_MinSecondsBetweenTrades   = 30;              // Cooldown seconds

//+------------------------------------------------------------------+
//| GLOBAL VARIABLES                                                  |
//+------------------------------------------------------------------+
CTrade         g_Trade;
CPositionInfo  g_Position;

int    g_BB_Handle    = INVALID_HANDLE;
int    g_RSI_Handle   = INVALID_HANDLE;
int    g_MA_Handle    = INVALID_HANDLE;

double g_PipSize      = 0.0;
double g_PeakEquity   = 0.0;
double g_DayStartBal  = 0.0;
datetime g_LastBarTime = 0;
datetime g_LastTradeCloseTime = 0;
bool   g_TradingDisabled = false;
datetime g_CurrentDayStart = 0;

datetime g_NewsTimes[];
int      g_NewsCount  = 0;
int      g_NewsMinutes = 15; // minutes before/after to block

//+------------------------------------------------------------------+
//| OnInit                                                            |
//+------------------------------------------------------------------+
int OnInit()
  {
   // Determine pip size (4-digit vs 5-digit broker)
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   g_PipSize  = (digits == 3 || digits == 5) ? _Point * 10.0 : _Point;

   // Create indicator handles
   g_BB_Handle  = iBands(_Symbol, PERIOD_M1, Inp_BB_Period, Inp_BB_Shift,
                          Inp_BB_Deviation, Inp_BB_Price);
   g_RSI_Handle = iRSI(_Symbol, PERIOD_M1, Inp_RSI_Period, Inp_RSI_Price);
   g_MA_Handle  = iMA(_Symbol, Inp_ContextTF, Inp_ContextMAPeriod, 0,
                       MODE_SMA, PRICE_CLOSE);

   if(g_BB_Handle  == INVALID_HANDLE ||
      g_RSI_Handle == INVALID_HANDLE ||
      g_MA_Handle  == INVALID_HANDLE)
     {
      Print("ERROR: Failed to create indicator handles. EA stopping.");
      return INIT_FAILED;
     }

   // Configure trade object
   g_Trade.SetExpertMagicNumber(Inp_MagicNumber);
   g_Trade.SetDeviationInPoints(Inp_Slippage);
   g_Trade.SetTypeFilling(ORDER_FILLING_FOK);

   // Initialise equity tracking
   g_PeakEquity    = AccountInfoDouble(ACCOUNT_EQUITY);
   g_DayStartBal   = AccountInfoDouble(ACCOUNT_BALANCE);
   g_CurrentDayStart = GetDayStart(TimeCurrent());

   // Load news filter file
   if(Inp_UseNewsFilter) LoadNewsFile();

   Print("BB_RSI_MeanReversion EA initialised. PipSize=", g_PipSize,
         " | Magic=", Inp_MagicNumber);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
//| OnDeinit                                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_BB_Handle  != INVALID_HANDLE) IndicatorRelease(g_BB_Handle);
   if(g_RSI_Handle != INVALID_HANDLE) IndicatorRelease(g_RSI_Handle);
   if(g_MA_Handle  != INVALID_HANDLE) IndicatorRelease(g_MA_Handle);
  }

//+------------------------------------------------------------------+
//| OnTick                                                            |
//+------------------------------------------------------------------+
void OnTick()
  {
   // 0. If globally disabled, just manage trailing on existing positions
   if(g_TradingDisabled)
     {
      if(Inp_UseTrailingStop) ManageTrailingStop();
      return;
     }

   // 1. Only act on new bar
   if(!IsNewBar()) return;

   // 2. Update peak equity
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(equity > g_PeakEquity) g_PeakEquity = equity;

   // 3. Reset day tracking if new day
   datetime today = GetDayStart(TimeCurrent());
   if(today != g_CurrentDayStart)
     {
      g_CurrentDayStart = today;
      g_DayStartBal     = AccountInfoDouble(ACCOUNT_BALANCE);
      Print("New trading day. Starting balance: ", g_DayStartBal);
     }

   // 4. Global drawdown check
   if(Inp_UseGlobalDDLimit && g_PeakEquity > 0)
     {
      double ddPct = (g_PeakEquity - equity) / g_PeakEquity * 100.0;
      if(ddPct >= Inp_GlobalDDPercent)
        {
         Print("GLOBAL DRAWDOWN LIMIT HIT: ", DoubleToString(ddPct, 2),
               "% >= ", Inp_GlobalDDPercent, "%. Halting EA.");
         if(Inp_CloseAllOnDD) CloseAllPositions();
         g_TradingDisabled = true;
         return;
        }
     }

   // 5. Daily loss check
   if(Inp_UseDailyLossLimit && g_DayStartBal > 0)
     {
      double dayLossPct = (g_DayStartBal - AccountInfoDouble(ACCOUNT_BALANCE))
                          / g_DayStartBal * 100.0;
      if(dayLossPct >= Inp_DailyLossPercent)
        {
         Print("DAILY LOSS LIMIT HIT: ", DoubleToString(dayLossPct, 2),
               "% >= ", Inp_DailyLossPercent, "%. Skipping until tomorrow.");
         return;
        }
     }

   // 6. Time filter
   if(Inp_UseTimeFilter && !IsTradeTime()) return;

   // 7. Spread filter
   double spreadPips = GetCurrentSpreadPips();
   if(spreadPips > Inp_MaxSpreadPips)
     {
      Print("Spread too high: ", DoubleToString(spreadPips, 2),
            " pips > max ", Inp_MaxSpreadPips);
      return;
     }

   // 8. News filter
   if(Inp_UseNewsFilter && IsNewsTime()) return;

   // 9. Cooldown check
   if((int)(TimeCurrent() - g_LastTradeCloseTime) < Inp_MinSecondsBetweenTrades)
      return;

   // 10. Get indicator values
   double bbUpper[], bbLower[], bbMiddle[];
   double rsiVal[];
   ArraySetAsSeries(bbUpper,  true);
   ArraySetAsSeries(bbLower,  true);
   ArraySetAsSeries(bbMiddle, true);
   ArraySetAsSeries(rsiVal,   true);

   int idx = Inp_CandlesSinceSignal; // 1 = last closed candle
   int need = idx + 2;

   if(CopyBuffer(g_BB_Handle, 1, 0, need, bbUpper)  < need) return; // Upper
   if(CopyBuffer(g_BB_Handle, 2, 0, need, bbLower)  < need) return; // Lower
   if(CopyBuffer(g_BB_Handle, 0, 0, need, bbMiddle) < need) return; // Middle
   if(CopyBuffer(g_RSI_Handle, 0, 0, need, rsiVal)  < need) return;

   double closePrice = iClose(_Symbol, PERIOD_M1, idx);
   double rsi        = rsiVal[idx];
   double bbUp       = bbUpper[idx];
   double bbLow      = bbLower[idx];

   // 11. M15 context
   double contextMA = 0.0;
   if(Inp_UseM15Context)
     {
      double maArr[];
      ArraySetAsSeries(maArr, true);
      if(CopyBuffer(g_MA_Handle, 0, 0, 2, maArr) < 2) return;
      contextMA = maArr[0];
     }

   // 12. Signal generation
   bool longSignal  = false;
   bool shortSignal = false;

   // Long
   if(Inp_AllowLong)
     {
      bool bbOk  = !Inp_RequireOutsideBand || (closePrice < bbLow);
      bool rsiOk = !Inp_RequireRSIFilter   || (rsi < Inp_RSI_Oversold);
      bool ctxOk = !Inp_UseM15Context      || (closePrice > contextMA - Inp_ContextMATol);
      longSignal = bbOk && rsiOk && ctxOk;
     }

   // Short
   if(Inp_AllowShort)
     {
      bool bbOk  = !Inp_RequireOutsideBand || (closePrice > bbUp);
      bool rsiOk = !Inp_RequireRSIFilter   || (rsi > Inp_RSI_Overbought);
      bool ctxOk = !Inp_UseM15Context      || (closePrice < contextMA + Inp_ContextMATol);
      shortSignal = bbOk && rsiOk && ctxOk;
     }

   // 13. Position check
   if(longSignal  && Inp_UseOnePositionPerDir && HasPositionInDirection(POSITION_TYPE_BUY))
      longSignal = false;
   if(shortSignal && Inp_UseOnePositionPerDir && HasPositionInDirection(POSITION_TYPE_SELL))
      shortSignal = false;

   // 14. Execute
   if(longSignal)
     {
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double sl  = ask - Inp_StopLossPips   * g_PipSize;
      double tp  = ask + Inp_TakeProfitPips * g_PipSize;
      sl = NormalizeDouble(sl, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
      tp = NormalizeDouble(tp, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
      double lot = CalculateLot(Inp_StopLossPips);
      OpenOrder(ORDER_TYPE_BUY, lot, ask, sl, tp);
     }
   else if(shortSignal)
     {
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double sl  = bid + Inp_StopLossPips   * g_PipSize;
      double tp  = bid - Inp_TakeProfitPips * g_PipSize;
      sl = NormalizeDouble(sl, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
      tp = NormalizeDouble(tp, (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS));
      double lot = CalculateLot(Inp_StopLossPips);
      OpenOrder(ORDER_TYPE_SELL, lot, bid, sl, tp);
     }

   // 15. Trailing stop management
   if(Inp_UseTrailingStop) ManageTrailingStop();
  }

//+------------------------------------------------------------------+
//| IsNewBar — returns true only once per M1 candle                  |
//+------------------------------------------------------------------+
bool IsNewBar()
  {
   datetime barTime = iTime(_Symbol, PERIOD_M1, 0);
   if(barTime == g_LastBarTime) return false;
   g_LastBarTime = barTime;
   return true;
  }

//+------------------------------------------------------------------+
//| IsTradeTime — returns true if current time is in session         |
//+------------------------------------------------------------------+
bool IsTradeTime()
  {
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int nowMins  = dt.hour * 60 + dt.min;
   int startMin = Inp_StartHour * 60 + Inp_StartMinute;
   int endMin   = Inp_EndHour   * 60 + Inp_EndMinute;
   return (nowMins >= startMin && nowMins < endMin);
  }

//+------------------------------------------------------------------+
//| GetCurrentSpreadPips                                              |
//+------------------------------------------------------------------+
double GetCurrentSpreadPips()
  {
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   return (ask - bid) / g_PipSize;
  }

//+------------------------------------------------------------------+
//| LoadNewsFile — parse news.txt (format: "YYYY.MM.DD HH:MM")      |
//+------------------------------------------------------------------+
void LoadNewsFile()
  {
   int fh = FileOpen(Inp_NewsFile, FILE_READ | FILE_TXT | FILE_COMMON);
   if(fh == INVALID_HANDLE)
     {
      Print("News file '", Inp_NewsFile, "' not found — news filter skipped.");
      return;
     }
   g_NewsCount = 0;
   ArrayResize(g_NewsTimes, 0);
   while(!FileIsEnding(fh))
     {
      string line = FileReadString(fh);
      StringTrimRight(line);
      StringTrimLeft(line);
      if(StringLen(line) < 16) continue;
      datetime t = StringToTime(line);
      if(t > 0)
        {
         ArrayResize(g_NewsTimes, g_NewsCount + 1);
         g_NewsTimes[g_NewsCount++] = t;
        }
     }
   FileClose(fh);
   Print("News filter loaded: ", g_NewsCount, " events from ", Inp_NewsFile);
  }

//+------------------------------------------------------------------+
//| IsNewsTime — returns true if within news window                  |
//+------------------------------------------------------------------+
bool IsNewsTime()
  {
   if(g_NewsCount == 0) return false;
   datetime now = TimeCurrent();
   int windowSec = g_NewsMinutes * 60;
   for(int i = 0; i < g_NewsCount; i++)
     {
      if(MathAbs((double)(now - g_NewsTimes[i])) <= windowSec)
         return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| CalculateLot — risk-based or fixed                               |
//+------------------------------------------------------------------+
double CalculateLot(int slPips)
  {
   if(Inp_UseFixedLot) return NormaliseLot(Inp_FixedLot);

   double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmt  = balance * Inp_RiskPercent / 100.0;
   double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);

   // pip value per lot in account currency
   double pipValuePerLot = (g_PipSize / tickSize) * tickVal;
   if(pipValuePerLot <= 0) return NormaliseLot(Inp_FixedLot);

   double rawLot = riskAmt / ((double)slPips * pipValuePerLot);
   return NormaliseLot(rawLot);
  }

//+------------------------------------------------------------------+
//| NormaliseLot — round to lot step, clamp to min/max              |
//+------------------------------------------------------------------+
double NormaliseLot(double lot)
  {
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double lotMin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double lotMax  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   lot = MathFloor(lot / lotStep) * lotStep;
   lot = MathMax(lot, lotMin);
   lot = MathMin(lot, lotMax);
   return NormalizeDouble(lot, 2);
  }

//+------------------------------------------------------------------+
//| OpenOrder — send with retry loop                                  |
//+------------------------------------------------------------------+
void OpenOrder(ENUM_ORDER_TYPE type, double lot, double price,
               double sl, double tp)
  {
   for(int attempt = 1; attempt <= Inp_MaxRetries; attempt++)
     {
      bool sent = false;
      if(type == ORDER_TYPE_BUY)
         sent = g_Trade.Buy(lot, _Symbol, price, sl, tp, Inp_TradeComment);
      else
         sent = g_Trade.Sell(lot, _Symbol, price, sl, tp, Inp_TradeComment);

      if(sent)
        {
         ulong ticket = g_Trade.ResultOrder();
         string dir   = (type == ORDER_TYPE_BUY) ? "BUY" : "SELL";
         Print(TimeToString(TimeCurrent()), " | ORDER OPENED | ", dir,
               " | Ticket=", ticket,
               " | Lot=", DoubleToString(lot, 2),
               " | Price=", DoubleToString(price, _Digits),
               " | SL=", DoubleToString(sl, _Digits),
               " | TP=", DoubleToString(tp, _Digits));
         return;
        }

      int err = GetLastError();
      Print("Order attempt ", attempt, " failed. Error=", err,
            " | Retcode=", g_Trade.ResultRetcode());

      // Don't retry on hard errors
      if(err == ERR_MARKET_CLOSED || err == ERR_TRADE_DISABLED) break;
      Sleep(500);
     }
   Print("Order FAILED after ", Inp_MaxRetries, " retries.");
  }

//+------------------------------------------------------------------+
//| HasPositionInDirection                                            |
//+------------------------------------------------------------------+
bool HasPositionInDirection(ENUM_POSITION_TYPE dir)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(g_Position.SelectByIndex(i))
        {
         if(g_Position.Magic() == Inp_MagicNumber &&
            g_Position.Symbol() == _Symbol &&
            g_Position.PositionType() == dir)
            return true;
        }
     }
   return false;
  }

//+------------------------------------------------------------------+
//| ManageTrailingStop                                                |
//+------------------------------------------------------------------+
void ManageTrailingStop()
  {
   double trailStart = Inp_TrailingStartPips * g_PipSize;
   double trailStep  = Inp_TrailingStepPips  * g_PipSize;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!g_Position.SelectByIndex(i)) continue;
      if(g_Position.Magic()  != Inp_MagicNumber) continue;
      if(g_Position.Symbol() != _Symbol) continue;

      double sl      = g_Position.StopLoss();
      double openPx  = g_Position.PriceOpen();
      double digits  = (double)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

      if(g_Position.PositionType() == POSITION_TYPE_BUY)
        {
         double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         double profit = bid - openPx;
         if(profit >= trailStart)
           {
            double newSL = NormalizeDouble(bid - trailStep, (int)digits);
            if(newSL > sl + _Point)
               g_Trade.PositionModify(g_Position.Ticket(), newSL,
                                      g_Position.TakeProfit());
           }
        }
      else // SELL
        {
         double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         double profit = openPx - ask;
         if(profit >= trailStart)
           {
            double newSL = NormalizeDouble(ask + trailStep, (int)digits);
            if(newSL < sl - _Point || sl == 0)
               g_Trade.PositionModify(g_Position.Ticket(), newSL,
                                      g_Position.TakeProfit());
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| CloseAllPositions                                                 |
//+------------------------------------------------------------------+
void CloseAllPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(g_Position.SelectByIndex(i))
        {
         if(g_Position.Magic() == Inp_MagicNumber &&
            g_Position.Symbol() == _Symbol)
           {
            g_Trade.PositionClose(g_Position.Ticket());
            Print(TimeToString(TimeCurrent()),
                  " | EMERGENCY CLOSE | Ticket=", g_Position.Ticket(),
                  " | Reason: Drawdown limit");
            g_LastTradeCloseTime = TimeCurrent();
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| GetDayStart — midnight of given datetime                         |
//+------------------------------------------------------------------+
datetime GetDayStart(datetime t)
  {
   MqlDateTime dt;
   TimeToStruct(t, dt);
   dt.hour = 0; dt.min = 0; dt.sec = 0;
   return StructToTime(dt);
  }

//+------------------------------------------------------------------+
//| OnTradeTransaction — track close time for cooldown              |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest     &request,
                        const MqlTradeResult      &result)
  {
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
     {
      if(trans.deal_type == DEAL_TYPE_BUY || trans.deal_type == DEAL_TYPE_SELL)
        {
         // Check if this deal closes a position
         if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY)
             == DEAL_ENTRY_OUT)
           {
            if((long)HistoryDealGetInteger(trans.deal, DEAL_MAGIC)
                == Inp_MagicNumber)
              {
               double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT);
               Print(TimeToString(TimeCurrent()),
                     " | POSITION CLOSED | Deal=", trans.deal,
                     " | Profit=", DoubleToString(profit, 2));
               g_LastTradeCloseTime = TimeCurrent();
              }
           }
        }
     }
  }
//+------------------------------------------------------------------+
